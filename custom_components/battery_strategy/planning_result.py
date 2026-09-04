"""Typed result and persistence codec for one planning refresh."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

from .const import COMMAND_IDLE, COMMAND_INPUT, COMMAND_OUTPUT
from .contracts import (
    BatteryConstraints,
    BatteryPlan,
    BatteryPlanSlot,
    PlanMode,
    SlotKey,
)
from .models import StrategyOptions
from .plan_models import DailyCost, PlanPoint, StrategyPlan

PERSISTED_PLAN_KEY = "_canonical_battery_plan_v1"
SLOT_MS = 15 * 60 * 1000


@dataclass(frozen=True, slots=True)
class PlanningResult:
    """Canonical executable plan plus its non-authoritative HA projection."""

    battery_plan: BatteryPlan | None
    operator_plan: StrategyPlan
    operator_data: Mapping[str, object]

    def __post_init__(self) -> None:
        """Defensively freeze the complete non-authoritative projection."""
        object.__setattr__(
            self,
            "operator_data",
            MappingProxyType(
                {key: _freeze(value) for key, value in self.operator_data.items()}
            ),
        )


def build_planning_result(
    battery_plan: BatteryPlan | None,
    operator_data: dict[str, object],
    *,
    timezone: dt.tzinfo,
    now_ms: int,
    override_active: bool,
) -> PlanningResult:
    """Build one immutable result without deriving executable intent from profiles."""
    return PlanningResult(
        battery_plan=battery_plan,
        operator_plan=operator_plan_from_output(
            operator_data,
            timezone=timezone,
            now_ms=now_ms,
            override_active=override_active,
        ),
        operator_data=operator_data,
    )


def persisted_output(result: PlanningResult) -> dict[str, object]:
    """Serialize the canonical plan beside, not inside, operator projection data."""
    return {
        **{key: _thaw(value) for key, value in result.operator_data.items()},
        PERSISTED_PLAN_KEY: (
            _serialize_battery_plan(result.battery_plan)
            if result.battery_plan is not None
            else None
        ),
    }


def result_from_persisted_output(
    output: Mapping[str, object],
    options: StrategyOptions,
    *,
    timezone: dt.tzinfo,
    now_ms: int | None = None,
) -> PlanningResult:
    """Restore a result; legacy/invalid snapshots retain display data but fail closed."""
    display_data = {
        key: value for key, value in output.items() if key != PERSISTED_PLAN_KEY
    }
    try:
        battery_plan = _deserialize_battery_plan(output.get(PERSISTED_PLAN_KEY))
        if battery_plan.constraints != _constraints_from_options(options):
            battery_plan = None
    except (KeyError, TypeError, ValueError):
        battery_plan = None
    return build_planning_result(
        battery_plan,
        display_data,
        timezone=timezone,
        now_ms=int(time.time() * 1000) if now_ms is None else now_ms,
        override_active=options.manual_mode != "off",
    )


def operator_plan_from_output(
    output: Mapping[str, object],
    *,
    timezone: dt.tzinfo,
    now_ms: int,
    override_active: bool,
) -> StrategyPlan:
    """Project persisted/operator data into the stable Home Assistant plan model."""
    points = _points_from_output(output, now_ms=now_ms, timezone=timezone)
    today = _date_from_points(points, 0)
    tomorrow = _date_from_points(points, 1)
    daily_costs = {}
    if today:
        daily_costs[today] = DailyCost(
            _float(output.get("baseline_cost_today_eur")),
            _float(output.get("optimized_cost_today_eur")),
        )
    if tomorrow:
        daily_costs[tomorrow] = DailyCost(
            _float(output.get("baseline_cost_tomorrow_eur")),
            _float(output.get("optimized_cost_tomorrow_eur")),
        )
    mode = _mode_to_command(
        str(output.get("mode") or output.get("planned_mode") or "idle")
    )
    return StrategyPlan(
        points=points,
        current_mode=mode,
        current_power_w=int(
            round(
                _float(
                    output.get("recommended_power_w", output.get("planned_power_w", 0))
                )
            )
        ),
        reason=str(output.get("reason") or "planning_pipeline"),
        daily_costs=daily_costs,
        price_stats={
            "min": _maybe_float(output.get("price_min_ct")),
            "max": _maybe_float(output.get("price_max_ct")),
            "avg": _maybe_float(output.get("price_avg_ct")),
            "p_low": _maybe_float(output.get("price_low_ct")),
            "p_high": _maybe_float(output.get("price_high_ct")),
            "terminal_value_ct": _maybe_float(output.get("terminal_value_ct")),
            "discharge_floor_ct": _maybe_float(output.get("discharge_floor_ct")),
        },
        load_forecast_next_1h_kwh=_float(output.get("load_forecast_next_1h_kwh")),
        pv_forecast_corrected_next_1h_kwh=_float(
            output.get("pv_forecast_corrected_next_1h_kwh")
        ),
        net_load_forecast_next_1h_kwh=_float(
            output.get("net_load_forecast_next_1h_kwh")
        ),
        grid_import_forecast_next_1h_kwh=_float(
            output.get("grid_import_forecast_next_1h_kwh")
        ),
        grid_export_forecast_next_1h_kwh=_float(
            output.get("grid_export_forecast_next_1h_kwh")
        ),
        virtual_soc_end_tomorrow_pct=_float(output.get("virtual_soc_end_tomorrow_pct")),
        override_active=override_active,
    )


def _serialize_battery_plan(plan: BatteryPlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "problem_id": plan.problem_id,
        "generated_at_ms": plan.generated_at_ms,
        "optimizer_version": plan.optimizer_version,
        "constraints": asdict(plan.constraints),
        "slots": [
            {
                **asdict(item),
                "slot": asdict(item.slot),
                "mode": item.mode.value,
            }
            for item in plan.slots
        ],
        "baseline_cost_eur": plan.baseline_cost_eur,
        "optimized_cost_eur": plan.optimized_cost_eur,
    }


def _deserialize_battery_plan(raw: object) -> BatteryPlan:
    if not isinstance(raw, dict):
        raise TypeError("canonical battery plan is missing")
    constraints_raw = raw["constraints"]
    slots_raw = raw["slots"]
    if not isinstance(constraints_raw, dict) or not isinstance(slots_raw, list):
        raise TypeError("canonical battery plan shape is invalid")
    slots = []
    for item in slots_raw:
        if not isinstance(item, dict) or not isinstance(item.get("slot"), dict):
            raise TypeError("canonical battery plan slot shape is invalid")
        slots.append(
            BatteryPlanSlot(
                slot=SlotKey(
                    int(item["slot"]["start_ms"]), int(item["slot"]["end_ms"])
                ),
                mode=PlanMode(str(item["mode"])),
                pv_charge_allowed=bool(item["pv_charge_allowed"]),
                grid_charge_allowed=bool(item["grid_charge_allowed"]),
                planned_charge_kwh=float(item["planned_charge_kwh"]),
                planned_discharge_kwh=float(item["planned_discharge_kwh"]),
                required_charge_kwh=float(item["required_charge_kwh"]),
                discharge_budget_kwh=float(item["discharge_budget_kwh"]),
                expected_soc_start_pct=float(item["expected_soc_start_pct"]),
                expected_soc_end_pct=float(item["expected_soc_end_pct"]),
                planned_pv_charge_kwh=float(item["planned_pv_charge_kwh"]),
                planned_grid_charge_kwh=float(item["planned_grid_charge_kwh"]),
            )
        )
    return BatteryPlan(
        plan_id=str(raw["plan_id"]),
        problem_id=str(raw["problem_id"]),
        generated_at_ms=int(raw["generated_at_ms"]),
        optimizer_version=str(raw["optimizer_version"]),
        constraints=BatteryConstraints(**constraints_raw),
        slots=tuple(slots),
        baseline_cost_eur=float(raw["baseline_cost_eur"]),
        optimized_cost_eur=float(raw["optimized_cost_eur"]),
    )


def _constraints_from_options(options: StrategyOptions) -> BatteryConstraints:
    """Return the physical constraints that a persisted plan must still satisfy."""
    return BatteryConstraints(
        capacity_kwh=max(0.5, float(options.battery_capacity_kwh)),
        min_soc_pct=float(options.min_soc_pct),
        max_soc_pct=float(options.max_soc_pct),
        max_charge_power_w=max(0.0, float(options.max_charge_power_w)),
        max_discharge_power_w=max(0.0, float(options.max_discharge_power_w)),
        round_trip_efficiency=float(options.round_trip_efficiency),
    )


def _points_from_output(
    output: Mapping[str, object],
    *,
    now_ms: int,
    timezone: dt.tzinfo,
) -> list[PlanPoint]:
    price = _series(output.get("profile_48h_price")) or _merge_series(
        _series(output.get("profile_today_price")),
        _series(output.get("profile_tomorrow_price")),
    )
    soc = _merge_series(
        _series(output.get("profile_today_soc")),
        _series(output.get("profile_tomorrow_soc")),
    )
    power = _merge_series(
        _series(output.get("profile_today_power")),
        _series(output.get("profile_tomorrow_power")),
    )
    charge = _series(output.get("profile_48h_charge_fc_power")) or _merge_series(
        _series(output.get("profile_today_charge_power")),
        _series(output.get("profile_tomorrow_charge_power")),
    )
    pv_charge = _series(output.get("profile_48h_pv_charge_fc_power")) or _merge_series(
        _series(output.get("profile_today_pv_charge_power")),
        _series(output.get("profile_tomorrow_pv_charge_power")),
    )
    grid_charge = _series(
        output.get("profile_48h_grid_charge_fc_power")
    ) or _merge_series(
        _series(output.get("profile_today_grid_charge_power")),
        _series(output.get("profile_tomorrow_grid_charge_power")),
    )
    required_charge = _series(
        output.get("profile_48h_required_charge_fc_power")
    ) or _merge_series(
        _series(output.get("profile_today_required_charge_power")),
        _series(output.get("profile_tomorrow_required_charge_power")),
    )
    discharge = _series(output.get("profile_48h_discharge_fc_power")) or _merge_series(
        _series(output.get("profile_today_discharge_power")),
        _series(output.get("profile_tomorrow_discharge_power")),
    )
    discharge_budget = _series(
        output.get("profile_48h_discharge_budget_kwh")
    ) or _merge_series(
        _series(output.get("profile_today_discharge_budget_kwh")),
        _series(output.get("profile_tomorrow_discharge_budget_kwh")),
    )
    pv = _series(output.get("profile_48h_pv_fc_power"))
    load = _series(output.get("profile_48h_house_fc_power"))
    grid_import = _series(output.get("profile_48h_grid_import_fc_power"))
    grid_export = _series(output.get("profile_48h_grid_export_fc_power"))
    grid_net = _series(output.get("profile_48h_grid_net_fc_power"))
    timestamps = sorted(
        {
            *load,
            *pv,
            *grid_import,
            *grid_export,
            *grid_net,
            *charge,
            *pv_charge,
            *grid_charge,
            *required_charge,
            *discharge,
            *soc,
            *power,
        }
    )
    points = []
    for ts_ms in timestamps:
        ch = _at(charge, ts_ms)
        dis = _at(discharge, ts_ms)
        forecast_surplus_w = max(0.0, _at(pv, ts_ms) - _at(load, ts_ms))
        explicit_sources = bool(pv_charge or grid_charge or required_charge)
        pv_charge_w = (
            _at(pv_charge, ts_ms) if explicit_sources else min(ch, forecast_surplus_w)
        )
        grid_charge_w = (
            _at(grid_charge, ts_ms) if explicit_sources else max(0.0, ch - pv_charge_w)
        )
        required_charge_w = (
            _at(required_charge, ts_ms)
            if explicit_sources
            else (ch if grid_charge_w > 0.0 else 0.0)
        )
        pow_w = _at(power, ts_ms) or max(ch, dis)
        mode = COMMAND_INPUT if ch > 0 else COMMAND_OUTPUT if dis > 0 else COMMAND_IDLE
        points.append(
            PlanPoint(
                ts_ms=ts_ms,
                date=dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone)
                .date()
                .isoformat(),
                price_ct=_at(price, ts_ms),
                load_fc_w=int(round(_at(load, ts_ms))),
                pv_fc_w=int(round(_at(pv, ts_ms))),
                grid_import_fc_w=int(round(_at(grid_import, ts_ms))),
                grid_export_fc_w=int(round(_at(grid_export, ts_ms))),
                grid_net_fc_w=int(round(_at(grid_net, ts_ms))),
                mode=mode,
                power_w=int(round(abs(pow_w))),
                charge_fc_w=int(round(ch)),
                discharge_fc_w=int(round(dis)),
                soc_pct=round(_at(soc, ts_ms), 2),
                discharge_budget_kwh=round(_at(discharge_budget, ts_ms), 3),
                pv_charge_fc_w=int(round(pv_charge_w)),
                grid_charge_fc_w=int(round(grid_charge_w)),
                required_charge_fc_w=int(round(required_charge_w)),
            )
        )
    return [point for point in points if point.ts_ms + SLOT_MS > now_ms]


def _series(raw: object) -> dict[int, float]:
    result = {}
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            result[int(float(item[0]))] = float(item[1])
        except (TypeError, ValueError):
            continue
    return result


def _merge_series(*series_items: dict[int, float]) -> dict[int, float]:
    merged: dict[int, float] = {}
    for series in series_items:
        merged.update(series)
    return merged


def _at(series: dict[int, float], ts_ms: int) -> float:
    if ts_ms in series:
        return float(series[ts_ms])
    if not series:
        return 0.0
    best = min(series, key=lambda item_ts: abs(item_ts - ts_ms))
    return float(series[best]) if abs(best - ts_ms) <= 20 * 60 * 1000 else 0.0


def _date_from_points(points: list[PlanPoint], index: int) -> str | None:
    dates = []
    for point in points:
        if point.date not in dates:
            dates.append(point.date)
    return dates[index] if len(dates) > index else None


def _mode_to_command(mode: str) -> str:
    if mode in ("charge_grid", "charge_pv_surplus", "charge_follow", "input"):
        return COMMAND_INPUT
    if mode.startswith("discharge") or mode == "output":
        return COMMAND_OUTPUT
    return COMMAND_IDLE


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _maybe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value

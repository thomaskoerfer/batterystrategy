"""Precomputed Home Assistant presentation for Battery Strategy entities."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .plan_models import StrategyPlan

PROFILE_ATTRIBUTE_KEYS = frozenset(
    {
        "price",
        "soc",
        "power",
        "charge_power",
        "discharge_power",
        "discharge_budget_kwh",
        "pv_fc_power",
        "house_fc_power",
        "pv_actual_power",
        "house_actual_power",
        "grid_net_actual_power",
        "columns",
        "rows",
    }
)


@dataclass(frozen=True, slots=True)
class OperatorProjection:
    """Immutable values and attributes consumed by Home Assistant entities."""

    values: Mapping[str, object]
    attributes: Mapping[str, Mapping[str, object]]

    def value(self, key: str) -> object | None:
        """Return one already-computed entity value."""
        return self.values.get(key)

    def attrs(self, key: str) -> dict[str, object] | None:
        """Return a defensive copy of already-computed entity attributes."""
        attrs = self.attributes.get(key)
        return dict(attrs) if attrs is not None else None


def build_operator_projection(
    data: Mapping[str, object],
    *,
    local_date: dt.date,
    timezone: str,
) -> OperatorProjection:
    """Build the complete operator-facing projection once per refresh."""
    command = data["command"]
    diagnostics = data["live_diagnostics"]
    inputs = data["inputs"]
    plan: StrategyPlan = data["plan"]  # type: ignore[assignment]
    directive = data["plan_to_live"]
    optimizer_attrs = data.get("optimizer_attrs") or {}
    today = local_date.isoformat()
    tomorrow = (local_date + dt.timedelta(days=1)).isoformat()

    charge_total_kwh = round(
        _raw_float(optimizer_attrs, "actual_battery_charge_grid_today_kwh")
        + _raw_float(optimizer_attrs, "actual_battery_charge_pv_today_kwh"),
        3,
    )
    discharge_total_kwh = _raw_float(
        optimizer_attrs, "actual_battery_discharge_credited_today_kwh"
    )
    values = {
        "mode": command.mode.value,
        "command_power": command.power_w,
        "command_source": _command_source(data),
        "reason": command.reason,
        "residual_with_ev": round(diagnostics.residual_with_ev_w),
        "residual_no_ev": round(diagnostics.residual_no_ev_w),
        "pv_surplus": round(diagnostics.pv_surplus_w),
        "allowed_discharge_load": round(diagnostics.allowed_discharge_load_w),
        "house_load_total": round(diagnostics.house_load_total_w),
        "house_load_no_ev": round(diagnostics.house_load_no_ev_w),
        "grid_import": round(inputs.grid_import_w),
        "grid_export": round(inputs.grid_export_w),
        "battery_power": round(inputs.battery_discharge_w - inputs.battery_charge_w),
        "ev_power": round(inputs.ev_charge_w),
        "soc": round(inputs.soc_pct, 1),
        "planned_mode": plan.current_mode,
        "planned_power": plan.current_power_w,
        "planned_charge_power": (
            plan.current_power_w if plan.current_mode == "input" else 0
        ),
        "planned_discharge_power": (
            plan.current_power_w if plan.current_mode == "output" else 0
        ),
        "plan_live_slot_start": _format_ts_ms(directive.slot.start_ms, timezone),
        "plan_live_slot_end": _format_ts_ms(directive.slot.end_ms, timezone),
        "plan_live_pv_charge_allowed": "on" if directive.pv_charge_allowed else "off",
        "plan_live_must_charge": round(directive.required_charge_power_w),
        "plan_live_must_charge_remaining": directive.required_charge_remaining_kwh,
        "plan_live_grid_charge_allowed": (
            "on" if directive.grid_charge_allowed else "off"
        ),
        "plan_live_discharge_budget": directive.discharge_budget_remaining_kwh,
        "optimizer_discharge_budget": _optimizer_discharge_budget_kwh(plan),
        "load_forecast_next_1h": plan.load_forecast_next_1h_kwh,
        "pv_forecast_corrected_next_1h": plan.pv_forecast_corrected_next_1h_kwh,
        "net_load_forecast_next_1h": plan.net_load_forecast_next_1h_kwh,
        "grid_import_forecast_next_1h": plan.grid_import_forecast_next_1h_kwh,
        "grid_export_forecast_next_1h": plan.grid_export_forecast_next_1h_kwh,
        "virtual_soc_end_tomorrow": round(plan.virtual_soc_end_tomorrow_pct, 1),
        "baseline_cost_today": _daily_cost(plan, today, "base_eur"),
        "optimized_cost_today": _daily_cost(plan, today, "with_bat_eur"),
        "estimated_savings_today": _daily_cost(plan, today, "saving_eur"),
        "baseline_cost_tomorrow": _daily_cost(plan, tomorrow, "base_eur"),
        "optimized_cost_tomorrow": _daily_cost(plan, tomorrow, "with_bat_eur"),
        "estimated_savings_tomorrow": _daily_cost(plan, tomorrow, "saving_eur"),
        "actual_savings_today": round(
            _raw_float(optimizer_attrs, "actual_savings_today_eur"), 3
        ),
        "actual_savings_cumulative": round(
            _raw_float(
                optimizer_attrs,
                "actual_savings_cumulative_eur",
                _raw_float(optimizer_attrs, "actual_savings_lifetime_eur"),
            ),
            3,
        ),
        "actual_charge_total_today": charge_total_kwh,
        "actual_charge_grid_today": round(
            _raw_float(optimizer_attrs, "actual_battery_charge_grid_today_kwh"), 3
        ),
        "actual_charge_pv_today": round(
            _raw_float(optimizer_attrs, "actual_battery_charge_pv_today_kwh"), 3
        ),
        "actual_avg_charge_price_today": _average_price(
            _raw_float(optimizer_attrs, "actual_battery_charge_cost_today_eur"),
            charge_total_kwh,
        ),
        "actual_discharge_credited_today": round(discharge_total_kwh, 3),
        "actual_avg_discharge_price_today": _average_price(
            _raw_float(optimizer_attrs, "actual_battery_discharge_credit_today_eur"),
            discharge_total_kwh,
        ),
        "profile_today": sum(point.date == today for point in plan.points),
        "profile_tomorrow": sum(point.date == tomorrow for point in plan.points),
        "profile_48h": len(plan.points),
        "plan_slots": len(plan.points),
    }
    attributes = {
        "soc": MappingProxyType(
            {
                "estimate_stale": bool(data.get("soc_estimate_stale", False)),
                "control_ready": bool(data.get("soc_control_ready", False)),
            }
        ),
        "profile_today": MappingProxyType(
            _profile_attrs(plan, optimizer_attrs, today, today, tomorrow)
        ),
        "profile_tomorrow": MappingProxyType(
            _profile_attrs(plan, optimizer_attrs, tomorrow, today, tomorrow)
        ),
        "profile_48h": MappingProxyType(
            _profile_attrs(plan, optimizer_attrs, None, today, tomorrow)
        ),
        "plan_slots": MappingProxyType(_plan_slot_attrs(plan)),
    }
    return OperatorProjection(
        values=MappingProxyType(values),
        attributes=MappingProxyType(attributes),
    )


def _command_source(data: Mapping[str, object]) -> str:
    if not data.get("strategy_enabled", False):
        return "external_control_strategy_disabled"
    if not data.get("send_commands", True):
        return "strategy_not_sending"
    return "battery_strategy"


def _raw_float(raw: object, key: str, default: float = 0.0) -> float:
    try:
        return float((raw if isinstance(raw, Mapping) else {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _average_price(cost_eur: float, energy_kwh: float) -> float:
    if energy_kwh <= 0.01:
        return 0.0
    return round(cost_eur / energy_kwh * 100.0, 1)


def _daily_cost(plan: StrategyPlan, date: str, field: str) -> float:
    value = plan.daily_costs.get(date)
    return getattr(value, field) if value is not None else 0.0


def _optimizer_discharge_budget_kwh(plan: StrategyPlan) -> float:
    if not plan.points:
        return 0.0
    return round(max(0.0, float(plan.points[0].discharge_budget_kwh)), 3)


def _plan_slot_attrs(plan: StrategyPlan) -> dict[str, object]:
    rows = []
    for point in plan.points:
        charge_w = max(0.0, float(point.charge_fc_w))
        pv_surplus_w = max(0.0, float(point.pv_fc_w) - float(point.load_fc_w))
        pv_charge_w = (
            min(charge_w, pv_surplus_w)
            if point.pv_charge_fc_w is None
            else max(0.0, float(point.pv_charge_fc_w))
        )
        grid_charge_w = (
            max(0.0, charge_w - pv_charge_w)
            if point.grid_charge_fc_w is None
            else max(0.0, float(point.grid_charge_fc_w))
        )
        required_charge_w = charge_w if grid_charge_w > 0.0 else 0.0
        if point.required_charge_fc_w is not None:
            required_charge_w = max(0.0, float(point.required_charge_fc_w))
        rows.append(
            [
                int(point.ts_ms // 1000),
                round(float(point.price_ct), 2),
                _slot_energy_kwh(
                    float(point.load_fc_w) - float(point.pv_fc_w), signed=True
                ),
                round(max(0.0, float(point.discharge_budget_kwh)), 3),
                _slot_energy_kwh(point.discharge_fc_w),
                _slot_energy_kwh(charge_w),
                _slot_energy_kwh(pv_charge_w),
                _slot_energy_kwh(grid_charge_w),
                _slot_energy_kwh(required_charge_w),
                round(float(point.soc_pct), 1),
            ]
        )
    return {
        "columns": [
            "slot_start",
            "price_ct_per_kwh",
            "planned_grid_net_before_battery_no_ev_kwh",
            "discharge_budget_kwh",
            "planned_discharge_kwh",
            "planned_charge_kwh",
            "planned_pv_charge_kwh",
            "planned_grid_charge_kwh",
            "required_charge_kwh",
            "planned_soc_pct",
        ],
        "rows": rows,
    }


def _slot_energy_kwh(power_w: float, *, signed: bool = False) -> float:
    value = float(power_w) * 0.25 / 1000.0
    return round(value if signed else max(0.0, value), 3)


def _profile_attrs(
    plan: StrategyPlan,
    raw: object,
    date: str | None,
    today: str,
    tomorrow: str,
) -> dict[str, object]:
    raw_attrs = _raw_profile_attrs(raw, date, today, tomorrow)
    if raw_attrs is not None:
        return raw_attrs
    return {
        "price": plan.profile("price_ct", date),
        "soc": plan.profile("soc_pct", date),
        "power": plan.profile("power_w", date),
        "charge_power": plan.profile("charge_fc_w", date),
        "discharge_power": plan.profile("discharge_fc_w", date),
        "discharge_budget_kwh": plan.profile("discharge_budget_kwh", date),
        "pv_fc_power": plan.profile("pv_fc_w", date),
        "house_fc_power": plan.profile("load_fc_w", date),
    }


def _raw_profile_attrs(
    raw: object,
    date: str | None,
    today: str,
    tomorrow: str,
) -> dict[str, object] | None:
    if not isinstance(raw, Mapping):
        return None
    if date is None:
        prefix = "profile_48h"
        key_map = {
            "pv_fc_power": "pv_fc_power",
            "house_fc_power": "house_fc_power",
            "pv_actual_power": "pv_actual_power",
            "house_actual_power": "house_actual_power",
            "grid_net_actual_power": "grid_net_actual_power",
        }
    elif date == today:
        prefix = "profile_today"
        key_map = {
            "price": "price",
            "soc": "soc",
            "pv_actual_power": "pv_actual_power",
            "house_actual_power": "house_actual_power",
        }
    elif date == tomorrow:
        prefix = "profile_tomorrow"
        key_map = {"price": "price", "soc": "soc"}
    else:
        return None
    attrs = {
        name: _profile(raw.get(f"{prefix}_{raw_key}"))
        for name, raw_key in key_map.items()
    }
    return attrs if any(attrs.values()) else None


def _profile(raw: object) -> list[list[float | int]]:
    out: list[list[float | int]] = []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            out.append([int(float(item[0])), float(item[1])])
        except (TypeError, ValueError):
            continue
    return out


def _format_ts_ms(ts_ms: int, timezone: str) -> str:
    if not ts_ms:
        return ""
    value = dt.datetime.fromtimestamp(ts_ms / 1000.0, dt.timezone.utc)
    return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")

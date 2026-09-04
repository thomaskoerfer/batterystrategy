"""Application orchestration for one immutable battery planning snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .const import COMMAND_IDLE, COMMAND_INPUT, COMMAND_OUTPUT
from .contracts import (
    BatteryConstraints,
    BatteryPlan,
    CommercialPolicy,
    ForecastBundle,
)
from .economic_optimizer import OPTIMIZER_VERSION
from .market_context import MarketContextService
from .optimization_problem import optimize_snapshot
from .plan_models import DailyCost, PlanPoint


@dataclass(frozen=True)
class PlanningSettings:
    """Setup-neutral physical and commercial settings for planning."""

    battery_capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    max_charge_power_w: float
    max_discharge_power_w: float
    round_trip_efficiency: float
    min_margin_ct_per_kwh: float
    export_opportunity_ct_per_kwh: float
    pv_charging_allowed: bool
    grid_charging_allowed: bool
    discharge_allowed: bool
    pv_recovery_confidence: float
    pv_recovery_reserve_kwh: float
    slot_hours: float = 0.25


@dataclass(frozen=True, slots=True)
class PlanningPublication:
    """Canonical optimizer plan and its presentation metadata."""

    battery_plan: BatteryPlan | None
    data: Mapping[str, object]
    operator_points: tuple[PlanPoint, ...]
    operator_daily_costs: Mapping[str, DailyCost]


class PlanningService:
    """Compose market policy, pure optimization and plan publication."""

    def __init__(
        self,
        *,
        market_context: MarketContextService,
        settings: PlanningSettings,
    ) -> None:
        self._market_context = market_context
        self._settings = settings

    def plan(
        self,
        *,
        intervals: list[dict],
        samples: list[dict],
        start_energy_kwh: float,
        forecast_bundle: ForecastBundle,
        eex_days: dict | None = None,
        forecast_diagnostics: dict | None = None,
    ) -> PlanningPublication:
        """Return canonical intent together with its presentation metadata."""
        metadata = self._market_context.build_plan_metadata(
            intervals,
            samples,
            eex_days=eex_days,
            forecast_diagnostics=forecast_diagnostics,
        )
        price_stats = metadata["price_stats"]
        constraints = BatteryConstraints(
            self._settings.battery_capacity_kwh,
            self._settings.min_soc_pct,
            self._settings.max_soc_pct,
            self._settings.max_charge_power_w,
            self._settings.max_discharge_power_w,
            self._settings.round_trip_efficiency,
        )
        policy = CommercialPolicy(
            min_margin_ct_per_kwh=self._settings.min_margin_ct_per_kwh,
            terminal_value_ct_per_kwh=float(
                price_stats.get("terminal_value_ct") or 0.0
            ),
            export_opportunity_ct_per_kwh=(
                self._settings.export_opportunity_ct_per_kwh
            ),
            discharge_floor_ct_per_kwh=price_stats.get("discharge_floor_ct"),
            pv_charging_allowed=self._settings.pv_charging_allowed,
            grid_charging_allowed=self._settings.grid_charging_allowed,
            discharge_allowed=self._settings.discharge_allowed,
            pv_recovery_confidence=self._settings.pv_recovery_confidence,
            pv_recovery_reserve_kwh=self._settings.pv_recovery_reserve_kwh,
        )
        if not intervals:
            return PlanningPublication(
                None,
                {
                    **metadata,
                    "points": [],
                    "end_soc": 0.0,
                    "daily_costs": {},
                    "optimizer_source": OPTIMIZER_VERSION,
                },
                (),
                {},
            )

        _, candidate = optimize_snapshot(
            intervals=intervals,
            forecast=forecast_bundle,
            start_energy_kwh=start_energy_kwh,
            constraints=constraints,
            policy=policy,
            evaluated_at_ms=int(intervals[0]["dt"].timestamp() * 1000),
        )
        return self._publish(
            candidate,
            intervals,
            forecast_bundle,
            metadata,
        )

    def _publish(
        self,
        candidate: BatteryPlan,
        intervals: list[dict],
        forecast_bundle: ForecastBundle,
        publication_metadata: dict,
    ) -> PlanningPublication:
        if not (
            len(candidate.slots)
            == len(intervals)
            == len(forecast_bundle.load.slots)
            == len(forecast_bundle.pv.slots)
        ):
            raise ValueError("pure plan and forecast grids differ")

        points = []
        operator_points = []
        daily: dict[str, dict[str, float]] = {}
        slot_hours = self._settings.slot_hours
        export_value = self._settings.export_opportunity_ct_per_kwh
        for interval, load_slot, pv_slot, plan_slot in zip(
            intervals,
            forecast_bundle.load.slots,
            forecast_bundle.pv.slots,
            candidate.slots,
            strict=True,
        ):
            if not (
                plan_slot.slot == load_slot.slot
                and plan_slot.slot == pv_slot.slot
                and plan_slot.slot.start_ms == int(interval["dt"].timestamp() * 1000)
            ):
                raise ValueError("pure plan slot does not match the published grid")
            date = interval["dt"].date().isoformat()
            price_ct = float(interval["price_eur"]) * 100.0
            load_kwh = max(0.0, load_slot.energy.p50_kwh)
            pv_kwh = max(0.0, pv_slot.energy.p50_kwh)
            net_load_kwh = max(0.0, load_kwh - pv_kwh)
            surplus_kwh = max(0.0, pv_kwh - load_kwh)
            charge_kwh = plan_slot.planned_charge_kwh
            discharge_kwh = plan_slot.planned_discharge_kwh
            pv_charge_kwh = plan_slot.planned_pv_charge_kwh
            grid_charge_kwh = plan_slot.planned_grid_charge_kwh
            grid_import_kwh = max(0.0, net_load_kwh - discharge_kwh) + grid_charge_kwh
            grid_export_kwh = max(0.0, surplus_kwh - pv_charge_kwh) + max(
                0.0, discharge_kwh - net_load_kwh
            )
            power_w = (charge_kwh - discharge_kwh) / slot_hours * 1000.0
            charge_w = charge_kwh / slot_hours * 1000.0
            discharge_w = discharge_kwh / slot_hours * 1000.0
            load_w = load_kwh / slot_hours * 1000.0
            pv_w = pv_kwh / slot_hours * 1000.0
            grid_import_w = grid_import_kwh / slot_hours * 1000.0
            grid_export_w = grid_export_kwh / slot_hours * 1000.0
            grid_net_w = grid_import_w - grid_export_w
            operator_point = PlanPoint(
                ts_ms=plan_slot.slot.start_ms,
                date=date,
                price_ct=round(price_ct, 3),
                load_fc_w=_operator_w(load_w),
                pv_fc_w=_operator_w(pv_w),
                grid_import_fc_w=_operator_w(grid_import_w),
                grid_export_fc_w=_operator_w(grid_export_w),
                grid_net_fc_w=_operator_w(grid_net_w),
                mode=(
                    COMMAND_INPUT
                    if charge_w > 0.0
                    else COMMAND_OUTPUT
                    if discharge_w > 0.0
                    else COMMAND_IDLE
                ),
                power_w=_operator_w(abs(power_w)),
                charge_fc_w=_operator_w(charge_w),
                discharge_fc_w=_operator_w(discharge_w),
                soc_pct=round(plan_slot.expected_soc_start_pct, 2),
                discharge_budget_kwh=round(plan_slot.discharge_budget_kwh, 3),
                pv_charge_fc_w=_operator_w(pv_charge_kwh / slot_hours * 1000.0),
                grid_charge_fc_w=_operator_w(grid_charge_kwh / slot_hours * 1000.0),
                required_charge_fc_w=_operator_w(
                    plan_slot.required_charge_kwh / slot_hours * 1000.0
                ),
            )
            point = {
                "ts_ms": plan_slot.slot.start_ms,
                "date": date,
                "price_ct": round(price_ct, 3),
                "soc_pct": round(plan_slot.expected_soc_start_pct, 2),
                "power_w": round(power_w, 1),
                "charge_fc_w": round(charge_w, 1),
                "pv_charge_fc_w": round(pv_charge_kwh / slot_hours * 1000.0, 1),
                "grid_charge_fc_w": round(grid_charge_kwh / slot_hours * 1000.0, 1),
                "required_charge_fc_w": round(
                    plan_slot.required_charge_kwh / slot_hours * 1000.0, 1
                ),
                "discharge_fc_w": round(discharge_w, 1),
                "discharge_budget_kwh": round(plan_slot.discharge_budget_kwh, 3),
                "mode": plan_slot.mode.value,
                "load_fc_w": round(load_w, 1),
                "pv_fc_w": round(pv_w, 1),
                "discharge_eligible_fc_w": round(net_load_kwh / slot_hours * 1000.0, 1),
                "grid_import_fc_w": round(grid_import_w, 1),
                "grid_export_fc_w": round(grid_export_w, 1),
                "grid_net_fc_w": round(grid_net_w, 1),
            }
            points.append(point)
            operator_points.append(operator_point)
            values = daily.setdefault(date, {"base": 0.0, "with_bat": 0.0})
            values["base"] += (
                net_load_kwh * price_ct - surplus_kwh * export_value
            ) / 100.0
            values["with_bat"] += (
                grid_import_kwh * price_ct - grid_export_kwh * export_value
            ) / 100.0

        daily_costs = {
            date: {
                "base_eur": round(values["base"], 3),
                "with_bat_eur": round(values["with_bat"], 3),
                "saving_eur": round(values["base"] - values["with_bat"], 3),
            }
            for date, values in daily.items()
        }
        today_date = (publication_metadata.get("today") or {}).get("date")
        tomorrow_date = (publication_metadata.get("tomorrow") or {}).get("date")
        result = dict(publication_metadata)
        result.update(
            {
                "points": points,
                "today": {
                    "date": today_date,
                    "saving_eur": daily_costs.get(today_date, {}).get(
                        "saving_eur", 0.0
                    ),
                },
                "tomorrow": {
                    "date": tomorrow_date,
                    "saving_eur": daily_costs.get(tomorrow_date, {}).get(
                        "saving_eur", 0.0
                    ),
                },
                "end_soc": round(
                    candidate.slots[-1].expected_soc_end_pct
                    if candidate.slots
                    else 0.0,
                    2,
                ),
                "daily_costs": daily_costs,
                "optimizer_source": candidate.optimizer_version,
            }
        )
        operator_daily_costs = {
            date: DailyCost(values["base_eur"], values["with_bat_eur"])
            for date, values in daily_costs.items()
        }
        return PlanningPublication(
            candidate, result, tuple(operator_points), operator_daily_costs
        )


def _operator_w(value: float) -> int:
    """Match the established one-decimal publication then integer projection."""
    return round(round(float(value), 1))

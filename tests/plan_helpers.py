"""Canonical plan fixtures shared by compiler-facing tests."""

from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    BatteryPlan,
    BatteryPlanSlot,
    PlanMode,
    SlotKey,
)
from custom_components.battery_strategy.models import StrategyOptions
from custom_components.battery_strategy.plan_models import StrategyPlan

SLOT_H = 0.25
SLOT_MS = 900_000


def canonical_plan(
    plan: StrategyPlan,
    options: StrategyOptions,
    generated_at_ms: int,
) -> BatteryPlan:
    """Build an explicit canonical plan fixture from concise test points."""
    constraints = BatteryConstraints(
        options.battery_capacity_kwh,
        options.min_soc_pct,
        options.max_soc_pct,
        options.max_charge_power_w,
        options.max_discharge_power_w,
        options.round_trip_efficiency,
    )
    slots = []
    for point in plan.points:
        charge_kwh = max(0.0, point.charge_fc_w) * SLOT_H / 1000.0
        discharge_budget_kwh = max(0.0, point.discharge_budget_kwh)
        discharge_kwh = min(
            max(0.0, point.discharge_fc_w) * SLOT_H / 1000.0,
            discharge_budget_kwh,
        )
        if point.pv_charge_fc_w is None and point.grid_charge_fc_w is None:
            surplus_w = max(0.0, point.pv_fc_w - point.load_fc_w)
            pv_charge_w = min(max(0.0, point.charge_fc_w), surplus_w)
            grid_charge_w = max(0.0, point.charge_fc_w - pv_charge_w)
        else:
            pv_charge_w = max(0.0, point.pv_charge_fc_w or 0.0)
            grid_charge_w = max(0.0, point.grid_charge_fc_w or 0.0)
        pv_charge_kwh = pv_charge_w * SLOT_H / 1000.0
        grid_charge_kwh = grid_charge_w * SLOT_H / 1000.0
        required_charge_w = (
            max(0.0, point.required_charge_fc_w)
            if point.required_charge_fc_w is not None
            else (max(0.0, point.charge_fc_w) if grid_charge_w > 0.0 else 0.0)
        )
        required_charge_kwh = required_charge_w * SLOT_H / 1000.0
        mode = (
            PlanMode.CHARGE
            if charge_kwh > 0.0
            else PlanMode.DISCHARGE
            if discharge_kwh > 0.0
            else PlanMode.IDLE
        )
        slots.append(
            BatteryPlanSlot(
                slot=SlotKey(point.ts_ms, point.ts_ms + SLOT_MS),
                mode=mode,
                pv_charge_allowed=True,
                grid_charge_allowed=grid_charge_kwh > 0.0,
                planned_charge_kwh=charge_kwh,
                planned_discharge_kwh=discharge_kwh,
                required_charge_kwh=required_charge_kwh,
                discharge_budget_kwh=discharge_budget_kwh,
                expected_soc_start_pct=point.soc_pct,
                expected_soc_end_pct=point.soc_pct,
                planned_pv_charge_kwh=pv_charge_kwh,
                planned_grid_charge_kwh=grid_charge_kwh,
            )
        )
    return BatteryPlan(
        plan_id=f"test-{generated_at_ms}",
        problem_id=f"test-{generated_at_ms}",
        generated_at_ms=generated_at_ms,
        optimizer_version="test",
        constraints=constraints,
        slots=tuple(slots),
        baseline_cost_eur=sum(item.base_eur for item in plan.daily_costs.values()),
        optimized_cost_eur=sum(item.with_bat_eur for item in plan.daily_costs.values()),
    )

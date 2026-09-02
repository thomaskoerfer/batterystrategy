"""Adapters between the published plan and the pure compiler contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .const import (
    DISCHARGE_LOAD,
    DISCHARGE_PRICE_SENSITIVE,
    GRID_CHARGING_PRICE_SENSITIVE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    PV_CHARGING_ON,
)
from .contracts import (
    BatteryConstraints,
    BatteryPlan,
    BatteryPlanSlot,
    PlanLiveDirective as ContractDirective,
    PlanMode,
    SlotKey,
)
from .contracts.common import SLOT_MS
from .models import StrategyOptions
from .plan_models import (
    PlanLiveDirective as PublishedDirective,
    PlanPoint,
    StrategyPlan,
)

SLOT_H = 0.25
ENERGY_EPSILON_KWH = 1e-9


def contract_plan_from_strategy_plan(
    plan: StrategyPlan,
    options: StrategyOptions,
    generated_at_ms: int,
) -> BatteryPlan:
    """Build the compiler contract from the stable published plan."""
    constraints = BatteryConstraints(
        capacity_kwh=max(0.5, float(options.battery_capacity_kwh)),
        min_soc_pct=float(options.min_soc_pct),
        max_soc_pct=float(options.max_soc_pct),
        max_charge_power_w=max(0.0, float(options.max_charge_power_w)),
        max_discharge_power_w=max(0.0, float(options.max_discharge_power_w)),
        round_trip_efficiency=float(options.round_trip_efficiency),
    )
    slots = tuple(
        _contract_slot(point, next_point, constraints, options)
        for point, next_point in zip(
            plan.points,
            [*plan.points[1:], None],
            strict=True,
        )
    )
    identity = hashlib.sha256(
        json.dumps(
            {
                "points": [asdict(point) for point in plan.points],
                "constraints": asdict(constraints),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    baseline = sum(item.base_eur for item in plan.daily_costs.values())
    optimized = sum(item.with_bat_eur for item in plan.daily_costs.values())
    return BatteryPlan(
        plan_id=f"published-{identity}",
        problem_id=f"published-{identity}",
        generated_at_ms=max(0, int(generated_at_ms)),
        optimizer_version="published-plan-adapter-v1",
        constraints=constraints,
        slots=slots,
        baseline_cost_eur=baseline,
        optimized_cost_eur=optimized,
    )


def published_directive_from_contract(
    directive: ContractDirective,
    plan: StrategyPlan,
    options: StrategyOptions,
) -> PublishedDirective:
    """Adapt compiler output to the established live-controller input."""
    point = next(
        (item for item in plan.points if item.ts_ms == directive.slot.start_ms),
        None,
    )
    manual = options.manual_mode in (MANUAL_CHARGE, MANUAL_DISCHARGE)
    pv_allowed = (
        directive.pv_charge_allowed
        and options.pv_charging == PV_CHARGING_ON
        and not manual
    )
    grid_allowed = (
        directive.grid_charge_allowed
        and options.grid_charging == GRID_CHARGING_PRICE_SENSITIVE
        and directive.required_charge_remaining_kwh > ENERGY_EPSILON_KWH
        and not manual
    )
    required_power_w = _required_charge_power_w(point)
    must_charge_w = (
        int(round(min(directive.max_grid_charge_power_w, required_power_w)))
        if grid_allowed
        else 0
    )
    if manual or must_charge_w > 0:
        discharge_budget_kwh = 0.0
    elif options.discharge == DISCHARGE_LOAD:
        # Load-following is an operator live policy, not commercial permission.
        # The established controller uses a positive value as an enable flag.
        discharge_budget_kwh = directive.max_discharge_power_w * SLOT_H / 1000.0
    elif options.discharge == DISCHARGE_PRICE_SENSITIVE:
        discharge_budget_kwh = directive.discharge_budget_remaining_kwh
    else:
        discharge_budget_kwh = 0.0
    return PublishedDirective(
        slot_id=str(directive.slot.start_ms),
        slot_start_ts=directive.slot.start_ms,
        slot_end_ts=directive.slot.end_ms,
        pv_charge_allowed=pv_allowed,
        must_charge_w=must_charge_w,
        must_charge_remaining_kwh=round(
            directive.required_charge_remaining_kwh if grid_allowed else 0.0,
            3,
        ),
        grid_charge_allowed=grid_allowed,
        discharge_budget_kwh=round(discharge_budget_kwh, 3),
        battery_min_soc_pct=directive.min_soc_pct,
        battery_max_soc_pct=directive.max_soc_pct,
    )


def closed_published_directive(
    options: StrategyOptions,
    *,
    slot_start_ms: int = 0,
) -> PublishedDirective:
    """Return a fail-closed directive without commercial permissions."""
    slot_start_ms = max(0, int(slot_start_ms))
    return PublishedDirective(
        slot_id=str(slot_start_ms) if slot_start_ms else "current",
        slot_start_ts=slot_start_ms,
        slot_end_ts=slot_start_ms + SLOT_MS if slot_start_ms else 0,
        pv_charge_allowed=False,
        must_charge_w=0,
        must_charge_remaining_kwh=0.0,
        grid_charge_allowed=False,
        discharge_budget_kwh=0.0,
        battery_min_soc_pct=float(options.min_soc_pct),
        battery_max_soc_pct=float(options.max_soc_pct),
    )


def _contract_slot(
    point: PlanPoint,
    next_point: PlanPoint | None,
    constraints: BatteryConstraints,
    options: StrategyOptions,
) -> BatteryPlanSlot:
    charge_kwh = _power_to_energy(point.charge_fc_w)
    planned_discharge_kwh = _power_to_energy(point.discharge_fc_w)
    # The published dashboard trajectory may contain a hypothetical discharge
    # while live permission is deliberately zero. The compiler contract carries
    # only executable flow, so clamp it to the explicit commercial budget.
    discharge_budget_kwh = max(0.0, float(point.discharge_budget_kwh))
    discharge_kwh = min(planned_discharge_kwh, discharge_budget_kwh)
    explicit_grid_w = getattr(point, "grid_charge_fc_w", None)
    if explicit_grid_w is None:
        pv_surplus_w = max(0.0, float(point.pv_fc_w) - float(point.load_fc_w))
        grid_charge_kwh = max(
            0.0, charge_kwh - _power_to_energy(min(point.charge_fc_w, pv_surplus_w))
        )
    else:
        grid_charge_kwh = min(charge_kwh, _power_to_energy(explicit_grid_w))
    pv_charge_kwh = max(0.0, charge_kwh - grid_charge_kwh)
    required_charge_kwh = min(
        charge_kwh,
        _power_to_energy(_required_charge_power_w(point)),
    )
    if grid_charge_kwh <= ENERGY_EPSILON_KWH:
        required_charge_kwh = 0.0
    mode = PlanMode.IDLE
    if charge_kwh > ENERGY_EPSILON_KWH:
        mode = PlanMode.CHARGE
    elif discharge_kwh > ENERGY_EPSILON_KWH:
        mode = PlanMode.DISCHARGE
    expected_end_soc = (
        float(next_point.soc_pct)
        if next_point is not None and next_point.ts_ms == point.ts_ms + SLOT_MS
        else _expected_end_soc(point, charge_kwh, discharge_kwh, constraints)
    )
    return BatteryPlanSlot(
        slot=SlotKey(int(point.ts_ms), int(point.ts_ms) + SLOT_MS),
        mode=mode,
        pv_charge_allowed=options.pv_charging == PV_CHARGING_ON,
        grid_charge_allowed=(
            options.grid_charging == GRID_CHARGING_PRICE_SENSITIVE
            and grid_charge_kwh > ENERGY_EPSILON_KWH
        ),
        planned_charge_kwh=charge_kwh,
        planned_discharge_kwh=discharge_kwh,
        required_charge_kwh=required_charge_kwh,
        discharge_budget_kwh=discharge_budget_kwh,
        expected_soc_start_pct=float(point.soc_pct),
        expected_soc_end_pct=expected_end_soc,
        planned_pv_charge_kwh=pv_charge_kwh,
        planned_grid_charge_kwh=grid_charge_kwh,
    )


def _required_charge_power_w(point: PlanPoint | None) -> float:
    if point is None:
        return 0.0
    explicit = getattr(point, "required_charge_fc_w", None)
    if explicit is not None:
        return max(0.0, float(explicit))
    return (
        max(0.0, float(point.charge_fc_w)) if _grid_charge_power_w(point) > 0 else 0.0
    )


def _grid_charge_power_w(point: PlanPoint) -> float:
    explicit = getattr(point, "grid_charge_fc_w", None)
    if explicit is not None:
        return max(0.0, float(explicit))
    surplus = max(0.0, float(point.pv_fc_w) - float(point.load_fc_w))
    return max(0.0, float(point.charge_fc_w) - surplus)


def _power_to_energy(power_w: float) -> float:
    return max(0.0, float(power_w)) * SLOT_H / 1000.0


def _expected_end_soc(
    point: PlanPoint,
    charge_kwh: float,
    discharge_kwh: float,
    constraints: BatteryConstraints,
) -> float:
    charge_efficiency = constraints.round_trip_efficiency**0.5
    discharge_efficiency = constraints.round_trip_efficiency**0.5
    energy = constraints.capacity_kwh * float(point.soc_pct) / 100.0
    energy += charge_kwh * charge_efficiency
    energy -= discharge_kwh / discharge_efficiency
    soc = 100.0 * energy / constraints.capacity_kwh
    return max(constraints.min_soc_pct, min(constraints.max_soc_pct, soc))

"""Projection adapter from compiler contracts to the established live model."""

from __future__ import annotations

from .const import (
    DISCHARGE_LOAD,
    DISCHARGE_PRICE_SENSITIVE,
    GRID_CHARGING_PRICE_SENSITIVE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    PV_CHARGING_ON,
)
from .contracts import BatteryPlan, BatteryPlanSlot
from .contracts import PlanLiveDirective as ContractDirective
from .contracts.common import SLOT_MS
from .models import StrategyOptions
from .plan_models import PlanLiveDirective as PublishedDirective

ENERGY_EPSILON_KWH = 1e-9


def published_directive_from_contract(
    directive: ContractDirective,
    plan: BatteryPlan,
    options: StrategyOptions,
) -> PublishedDirective:
    """Project a canonical compiler directive into the current live interface."""
    plan_slot = next(
        (item for item in plan.slots if item.slot == directive.slot),
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
    required_power_w = _required_charge_power_w(plan_slot)
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
        discharge_budget_kwh = directive.max_discharge_power_w * 0.25 / 1000.0
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
    allow_pv_charge: bool = False,
) -> PublishedDirective:
    """Return a directive without commercial charge or discharge permission."""
    slot_start_ms = max(0, int(slot_start_ms))
    return PublishedDirective(
        slot_id=str(slot_start_ms) if slot_start_ms else "current",
        slot_start_ts=slot_start_ms,
        slot_end_ts=slot_start_ms + SLOT_MS if slot_start_ms else 0,
        pv_charge_allowed=allow_pv_charge and options.pv_charging == PV_CHARGING_ON,
        must_charge_w=0,
        must_charge_remaining_kwh=0.0,
        grid_charge_allowed=False,
        discharge_budget_kwh=0.0,
        battery_min_soc_pct=float(options.min_soc_pct),
        battery_max_soc_pct=float(options.max_soc_pct),
    )


def _required_charge_power_w(slot: BatteryPlanSlot | None) -> float:
    if slot is None:
        return 0.0
    return max(0.0, slot.required_charge_kwh) / 0.25 * 1000.0

"""Pure Battery Strategy command calculation.

This module intentionally has no Home Assistant imports. It is the first
parallel-run core for the HACS integration and covers the currently used mode:
no grid charging and load-following discharge.
"""

from __future__ import annotations

from .const import (
    COMMAND_IDLE,
    COMMAND_INPUT,
    COMMAND_OUTPUT,
    DISCHARGE_LOAD,
    DISCHARGE_PRICE_SENSITIVE,
    GRID_CHARGING_PRICE_SENSITIVE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    MANUAL_OFF,
    PV_CHARGING_ON,
)
from .models import StrategyCommand, StrategyInputs, StrategyOptions
from .plan_models import PlanLiveDirective, StrategyPlan

BATTERY_CAPACITY_KWH = 6.0
SLOT_H = 0.25
PRICE_VALUE_TIE_CT = 0.5


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value to an inclusive range."""
    return max(low, min(high, value))


def net_no_battery_with_ev_w(inputs: StrategyInputs) -> float:
    """Net grid demand reconstructed without battery influence, EV included."""
    return float(inputs.grid_import_w) - float(inputs.grid_export_w) + float(inputs.battery_power_w)


def active_ev_power_w(inputs: StrategyInputs, options: StrategyOptions) -> float:
    """Return EV power only when it exceeds the configured active threshold."""
    ev_power = max(0.0, float(inputs.ev_power_w))
    return ev_power if ev_power >= float(options.ev_active_threshold_w) else 0.0


def net_no_battery_no_ev_w(inputs: StrategyInputs, options: StrategyOptions) -> float:
    """Net grid demand reconstructed without battery and configured EV influence."""
    return net_no_battery_with_ev_w(inputs) - active_ev_power_w(inputs, options)


def real_pv_surplus_w(inputs: StrategyInputs, options: StrategyOptions) -> float:
    """PV surplus available for strategy charging."""
    residual = net_no_battery_with_ev_w(inputs)
    if not options.pv_to_ev_first:
        residual -= active_ev_power_w(inputs, options)
    return max(0.0, -residual)


def allowed_discharge_load_w(inputs: StrategyInputs, options: StrategyOptions) -> float:
    """Load that automatic discharge may serve."""
    residual = net_no_battery_with_ev_w(inputs)
    if not options.battery_may_feed_ev:
        residual -= active_ev_power_w(inputs, options)
    return max(0.0, residual)


def current_house_loads_w(inputs: StrategyInputs, options: StrategyOptions) -> tuple[float, float]:
    """Return total house load and house load excluding active EV."""
    total = max(
        0.0,
        float(inputs.grid_import_w)
        + float(inputs.pv_w)
        + float(inputs.battery_power_w)
        - float(inputs.grid_export_w),
    )
    return total, max(0.0, total - active_ev_power_w(inputs, options))


def apply_minimum_power(power_w: float, options: StrategyOptions) -> int:
    """Round small commands to zero and return an integer Watt command."""
    if power_w < float(options.min_command_power_w):
        return 0
    return int(round(power_w))


def calculate_command(inputs: StrategyInputs, options: StrategyOptions) -> StrategyCommand:
    """Calculate the current battery command for the minimal parallel strategy."""
    residual_with_ev = net_no_battery_with_ev_w(inputs)
    residual_no_ev = net_no_battery_no_ev_w(inputs, options)
    pv_surplus = real_pv_surplus_w(inputs, options)
    discharge_load = allowed_discharge_load_w(inputs, options)
    house_total, house_no_ev = current_house_loads_w(inputs, options)

    mode = COMMAND_IDLE
    power = 0.0
    reason = "idle"

    if options.manual_mode == MANUAL_CHARGE:
        if float(inputs.soc_pct) >= float(options.max_soc_pct):
            reason = "max_soc"
        else:
            mode = COMMAND_INPUT
            power = clamp(float(options.manual_power_w), 0.0, float(options.max_charge_power_w))
            reason = "manual_charge"
    elif options.manual_mode == MANUAL_DISCHARGE:
        if float(inputs.soc_pct) <= float(options.min_soc_pct):
            reason = "min_soc"
        else:
            mode = COMMAND_OUTPUT
            power = clamp(float(options.manual_power_w), 0.0, float(options.max_discharge_power_w))
            reason = "manual_discharge"
    elif float(inputs.soc_pct) <= float(options.min_soc_pct):
        reason = "min_soc"
    elif (
        options.discharge == DISCHARGE_LOAD
        and discharge_load > 0.0
        and options.manual_mode == MANUAL_OFF
    ):
        mode = COMMAND_OUTPUT
        power = clamp(discharge_load, 0.0, float(options.max_discharge_power_w))
        reason = "load_discharge"
    elif (
        options.pv_charging == PV_CHARGING_ON
        and pv_surplus > 0.0
        and float(inputs.soc_pct) < float(options.max_soc_pct)
    ):
        mode = COMMAND_INPUT
        power = clamp(pv_surplus, 0.0, float(options.max_charge_power_w))
        reason = "pv_surplus"
    elif options.grid_charging == GRID_CHARGING_PRICE_SENSITIVE:
        reason = "price_optimizer_pending"

    command_power = apply_minimum_power(power, options)
    if command_power == 0:
        mode = COMMAND_IDLE

    return StrategyCommand(
        mode=mode,
        power_w=command_power,
        reason=reason,
        residual_with_ev_w=int(round(residual_with_ev)),
        residual_no_ev_w=int(round(residual_no_ev)),
        pv_surplus_w=int(round(pv_surplus)),
        allowed_discharge_load_w=int(round(discharge_load)),
        house_load_total_w=int(round(house_total)),
        house_load_no_ev_w=int(round(house_no_ev)),
    )


def live_command_from_plan(
    plan: StrategyPlan,
    live_command: StrategyCommand,
    inputs: StrategyInputs,
    options: StrategyOptions,
) -> StrategyCommand:
    """Return a safe live command using the current plan directive."""
    directive = plan_live_directive_from_plan(plan, options)
    return live_command_from_directive(directive, live_command, inputs, options)


def plan_live_directive_from_plan(plan: StrategyPlan, options: StrategyOptions) -> PlanLiveDirective:
    """Translate the optimizer plan into the minimal live-control directive."""
    manual = options.manual_mode in (MANUAL_CHARGE, MANUAL_DISCHARGE)
    current_point = plan.points[0] if plan.points else None
    slot_start_ts = int(current_point.ts_ms) if current_point is not None else 0
    slot_end_ts = slot_start_ts + int(SLOT_H * 3600 * 1000) if slot_start_ts else 0
    slot_id = str(slot_start_ts) if slot_start_ts else "current"
    pv_charge_allowed = options.pv_charging == PV_CHARGING_ON and not manual
    planned_charge_w = _planned_charge_w(plan)
    planned_pv_surplus_w = (
        max(0.0, float(current_point.pv_fc_w) - float(current_point.load_fc_w))
        if current_point is not None
        else 0.0
    )
    required_grid_charge_w = _deadline_required_grid_charge_w(plan, options)
    grid_charge_allowed = bool(
        options.grid_charging == GRID_CHARGING_PRICE_SENSITIVE
        and required_grid_charge_w >= float(options.min_command_power_w)
        and not manual
    )
    must_charge_w = (
        int(round(min(float(options.max_charge_power_w), planned_pv_surplus_w + required_grid_charge_w)))
        if grid_charge_allowed
        else 0
    )
    must_charge_remaining_kwh = _power_w_to_slot_kwh(must_charge_w) if grid_charge_allowed else 0.0

    discharge_budget_kwh = 0.0
    if not manual and options.discharge == DISCHARGE_LOAD:
        discharge_budget_kwh = _power_w_to_slot_kwh(float(options.max_discharge_power_w))
    elif not manual and options.discharge == DISCHARGE_PRICE_SENSITIVE:
        discharge_budget_kwh = _planned_discharge_budget_kwh(plan, options) if must_charge_w <= 0 else 0.0

    return PlanLiveDirective(
        slot_id=slot_id,
        slot_start_ts=slot_start_ts,
        slot_end_ts=slot_end_ts,
        pv_charge_allowed=pv_charge_allowed,
        must_charge_w=must_charge_w,
        must_charge_remaining_kwh=must_charge_remaining_kwh,
        grid_charge_allowed=grid_charge_allowed,
        discharge_budget_kwh=round(discharge_budget_kwh, 3),
        battery_min_soc_pct=float(options.min_soc_pct),
        battery_max_soc_pct=float(options.max_soc_pct),
    )


def live_command_from_directive(
    directive: PlanLiveDirective,
    live_command: StrategyCommand,
    inputs: StrategyInputs,
    options: StrategyOptions,
) -> StrategyCommand:
    """Apply the plan directive to the current meter-following diagnostics."""
    if options.manual_mode in (MANUAL_CHARGE, MANUAL_DISCHARGE):
        return _with_reason(live_command, live_command.reason)

    if (
        directive.must_charge_w > 0
        and directive.must_charge_remaining_kwh > 0.0
        and directive.grid_charge_allowed
        and float(inputs.soc_pct) < float(directive.battery_max_soc_pct)
    ):
        pv_part_w = float(live_command.pv_surplus_w) if directive.pv_charge_allowed else 0.0
        grid_part_w = max(0.0, float(directive.must_charge_w) - pv_part_w)
        power = min(pv_part_w + grid_part_w, float(options.max_charge_power_w))
        return _command_like(live_command, COMMAND_INPUT, power, "must_charge")

    if (
        directive.pv_charge_allowed
        and live_command.pv_surplus_w > 0
        and float(inputs.soc_pct) < float(directive.battery_max_soc_pct)
    ):
        power = min(float(live_command.pv_surplus_w), float(options.max_charge_power_w))
        return _command_like(live_command, COMMAND_INPUT, power, "live_pv_surplus")

    if (
        directive.discharge_budget_kwh > 0.0
        and live_command.allowed_discharge_load_w > 0
        and float(inputs.soc_pct) > float(directive.battery_min_soc_pct)
    ):
        power = min(float(live_command.allowed_discharge_load_w), float(options.max_discharge_power_w))
        return _command_like(live_command, COMMAND_OUTPUT, power, "budget_discharge")

    if float(inputs.soc_pct) <= float(directive.battery_min_soc_pct):
        return _idle_like(live_command, "min_soc")

    return _idle_like(live_command, "live_idle")


def _command_like(diagnostics: StrategyCommand, mode: str, power_w: float, reason: str) -> StrategyCommand:
    power = apply_minimum_power(max(0.0, power_w), StrategyOptions(min_command_power_w=0.0))
    if power <= 0:
        mode = COMMAND_IDLE
    return StrategyCommand(
        mode=mode,
        power_w=power,
        reason=reason,
        residual_with_ev_w=diagnostics.residual_with_ev_w,
        residual_no_ev_w=diagnostics.residual_no_ev_w,
        pv_surplus_w=diagnostics.pv_surplus_w,
        allowed_discharge_load_w=diagnostics.allowed_discharge_load_w,
        house_load_total_w=diagnostics.house_load_total_w,
        house_load_no_ev_w=diagnostics.house_load_no_ev_w,
    )


def _idle_like(diagnostics: StrategyCommand, reason: str) -> StrategyCommand:
    return _command_like(diagnostics, COMMAND_IDLE, 0.0, reason)


def _with_reason(command: StrategyCommand, reason: str) -> StrategyCommand:
    return _command_like(command, command.mode, command.power_w, reason)


def _planned_charge_w(plan: StrategyPlan) -> float:
    point = plan.points[0] if plan.points else None
    if point is not None:
        return max(0.0, float(point.charge_fc_w))
    return float(plan.current_power_w) if plan.current_mode == COMMAND_INPUT else 0.0


def _planned_discharge_w(plan: StrategyPlan) -> float:
    point = plan.points[0] if plan.points else None
    if point is not None:
        return max(0.0, float(point.discharge_fc_w))
    return float(plan.current_power_w) if plan.current_mode == COMMAND_OUTPUT else 0.0


def _planned_discharge_budget_kwh(plan: StrategyPlan, options: StrategyOptions) -> float:
    point = plan.points[0] if plan.points else None
    if point is None:
        return 0.0
    budget = max(0.0, float(getattr(point, "discharge_budget_kwh", 0.0)))
    available = _available_above_min_kwh(float(point.soc_pct), options)
    return round(min(available, budget), 3)


def _deadline_required_grid_charge_w(plan: StrategyPlan, options: StrategyOptions) -> float:
    """Return grid charge power that must happen in the current slot to meet the cheap-block plan."""
    if not plan.points:
        return 0.0

    current = plan.points[0]
    current_grid_kwh = _planned_grid_charge_kwh(current)
    if current_grid_kwh <= 0.0:
        return 0.0

    current_price = float(current.price_ct)
    block = _grid_charge_deadline_block(plan, current_price)
    required_grid_kwh = sum(_planned_grid_charge_kwh(point) for point in block)
    future_grid_capacity_kwh = sum(_grid_charge_capacity_kwh(point, options) for point in block[1:])
    required_now_kwh = max(0.0, required_grid_kwh - future_grid_capacity_kwh)
    return min(current_grid_kwh, required_now_kwh) / SLOT_H * 1000.0


def _grid_charge_deadline_block(plan: StrategyPlan, current_price_ct: float):
    """Return the contiguous block where grid charge can be deferred without worse pricing."""
    block = []
    for point in plan.points:
        if float(point.price_ct) > current_price_ct + PRICE_VALUE_TIE_CT:
            break
        block.append(point)
    return block


def _planned_grid_charge_kwh(point) -> float:
    planned_charge_w = max(0.0, float(point.charge_fc_w))
    planned_pv_surplus_w = max(0.0, float(point.pv_fc_w) - float(point.load_fc_w))
    return _power_w_to_slot_kwh_precise(max(0.0, planned_charge_w - planned_pv_surplus_w))


def _grid_charge_capacity_kwh(point, options: StrategyOptions) -> float:
    planned_pv_surplus_w = max(0.0, float(point.pv_fc_w) - float(point.load_fc_w))
    capacity_w = max(0.0, float(options.max_charge_power_w) - planned_pv_surplus_w)
    return _power_w_to_slot_kwh_precise(capacity_w)


def _power_w_to_slot_kwh_precise(power_w: float) -> float:
    return max(0.0, float(power_w)) * SLOT_H / 1000.0


def _power_w_to_slot_kwh(power_w: float) -> float:
    return round(max(0.0, float(power_w)) * SLOT_H / 1000.0, 3)


def _available_above_min_kwh(soc_pct: float, options: StrategyOptions) -> float:
    return BATTERY_CAPACITY_KWH * max(0.0, float(soc_pct) - float(options.min_soc_pct)) / 100.0

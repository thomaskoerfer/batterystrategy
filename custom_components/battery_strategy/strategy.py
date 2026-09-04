"""Pure live controller at the plan-to-actuator seam."""

from __future__ import annotations

from .contracts import (
    AutomaticDischargeMode,
    BatteryCommand,
    CommandMode,
    LiveControlResult,
    LiveControlState,
    LiveDiagnostics,
    LiveMeasurements,
    LivePolicy,
    ManualControlMode,
    PlanLiveDirective,
    QualityFlag,
)

POWER_TOLERANCE_W = 5.0
POWER_START_W = 50.0
CHARGE_RESTART_FAST_MS = 2_000
CHARGE_RESTART_SLOW_MS = 60_000
CHARGE_RECENT_WINDOW_MS = 300_000


def _active_ev_power_w(measurements: LiveMeasurements, policy: LivePolicy) -> float:
    return (
        measurements.ev_charge_w
        if measurements.ev_charge_w >= policy.ev_active_threshold_w
        else 0.0
    )


def _diagnostics(measurements: LiveMeasurements, policy: LivePolicy) -> LiveDiagnostics:
    battery_net_w = measurements.battery_discharge_w - measurements.battery_charge_w
    signed_residual_w = (
        measurements.grid_import_w - measurements.grid_export_w + battery_net_w
    )
    ev_power_w = _active_ev_power_w(measurements, policy)
    pv_residual_w = signed_residual_w
    if not policy.pv_to_ev_first:
        # The battery may intentionally compete with an active EV in this mode.
        pv_residual_w -= ev_power_w
    allowed_discharge_load_w = signed_residual_w
    if ev_power_w > 0.0 and not policy.discharge_during_ev_charging:
        allowed_discharge_load_w = 0.0
    elif not policy.battery_may_feed_ev:
        allowed_discharge_load_w -= ev_power_w
    house_load_total_w = max(
        0.0,
        measurements.grid_import_w
        + measurements.pv_generation_w
        + battery_net_w
        - measurements.grid_export_w,
    )
    return LiveDiagnostics(
        residual_with_ev_w=signed_residual_w,
        residual_no_ev_w=signed_residual_w - ev_power_w,
        pv_surplus_w=max(0.0, -pv_residual_w),
        allowed_discharge_load_w=max(0.0, allowed_discharge_load_w),
        house_load_total_w=house_load_total_w,
        house_load_no_ev_w=max(0.0, house_load_total_w - ev_power_w),
    )


class DeterministicLiveController:
    """Turn one directive and one measurement snapshot into one command."""

    def command(
        self,
        directive: PlanLiveDirective,
        measurements: LiveMeasurements,
        policy: LivePolicy,
        state: LiveControlState,
    ) -> LiveControlResult:
        """Evaluate approved precedence without I/O or economic reinterpretation."""
        diagnostics = _diagnostics(measurements, policy)
        directive_current = (
            directive.slot.start_ms
            <= measurements.captured_at_ms
            < directive.slot.end_ms
        )
        mode = CommandMode.IDLE
        power_w = 0.0
        reason = "live_idle"

        safety_reason = _safety_reason(measurements, policy)
        if safety_reason is not None:
            reason = safety_reason
        elif policy.manual_mode == ManualControlMode.CHARGE:
            if measurements.soc_pct >= directive.max_soc_pct:
                reason = "max_soc"
            else:
                mode = CommandMode.INPUT
                power_w = min(
                    policy.manual_power_w,
                    policy.max_charge_power_w,
                )
                reason = "manual_charge"
        elif policy.manual_mode == ManualControlMode.DISCHARGE:
            if measurements.soc_pct <= directive.min_soc_pct:
                reason = "min_soc"
            else:
                mode = CommandMode.OUTPUT
                power_w = min(policy.manual_power_w, policy.max_discharge_power_w)
                reason = "manual_discharge"
        elif not directive_current:
            reason = "directive_outside_slot"
        elif (
            directive.required_charge_power_w > 0.0
            and directive.required_charge_remaining_kwh > 0.0
            and directive.grid_charge_allowed
            and measurements.soc_pct < directive.max_soc_pct
        ):
            mode = CommandMode.INPUT
            power_w = min(
                max(
                    directive.required_charge_power_w,
                    diagnostics.pv_surplus_w if directive.pv_charge_allowed else 0.0,
                ),
                directive.max_grid_charge_power_w,
            )
            reason = "must_charge"
        elif (
            directive.pv_charge_allowed
            and diagnostics.pv_surplus_w > 0.0
            and measurements.soc_pct < directive.max_soc_pct
        ):
            mode = CommandMode.INPUT
            power_w = min(diagnostics.pv_surplus_w, directive.max_pv_charge_power_w)
            reason = "live_pv_surplus"
        elif measurements.soc_pct <= directive.min_soc_pct:
            reason = "min_soc"
        elif (
            policy.automatic_discharge_mode != AutomaticDischargeMode.OFF
            and _active_ev_power_w(measurements, policy) > 0.0
            and not policy.discharge_during_ev_charging
        ):
            reason = "ev_discharge_blocked"
        elif self._discharge_permitted(directive, policy):
            mode = CommandMode.OUTPUT
            power_w = min(
                diagnostics.allowed_discharge_load_w,
                directive.max_discharge_power_w,
            )
            reason = (
                "load_discharge"
                if policy.automatic_discharge_mode
                == AutomaticDischargeMode.LOAD_FOLLOWING
                else "budget_discharge"
            )

        if power_w < policy.min_command_power_w:
            mode = CommandMode.IDLE
            power_w = 0.0

        command = BatteryCommand(
            command_id=(
                f"{directive.directive_id}:{measurements.captured_at_ms}:"
                f"{mode.value}:{round(power_w)}"
            ),
            directive_id=directive.directive_id,
            created_at_ms=measurements.captured_at_ms,
            valid_until_ms=(
                directive.slot.end_ms
                if directive_current
                else measurements.captured_at_ms + 30_000
            ),
            mode=mode,
            power_w=float(round(power_w)),
            reason=reason,
        )
        command, direction, block_until_ms, last_charge_at_ms = _apply_hysteresis(
            command, measurements, state
        )
        next_state = LiveControlState(
            previous_mode=command.mode,
            previous_power_w=command.power_w,
            previous_command_at_ms=command.created_at_ms,
            direction=direction,
            charge_block_until_ms=block_until_ms,
            last_charge_at_ms=last_charge_at_ms,
        )
        return LiveControlResult(command, next_state, diagnostics)

    @staticmethod
    def _discharge_permitted(directive: PlanLiveDirective, policy: LivePolicy) -> bool:
        if policy.automatic_discharge_mode == AutomaticDischargeMode.LOAD_FOLLOWING:
            return True
        if policy.automatic_discharge_mode == AutomaticDischargeMode.PRICE_SENSITIVE:
            return directive.discharge_budget_remaining_kwh > 0.0
        return False


def _safety_reason(measurements: LiveMeasurements, policy: LivePolicy) -> str | None:
    flags = set(measurements.quality.flags)
    if QualityFlag.ESTIMATED in flags:
        return "battery_soc_unavailable"
    if QualityFlag.MISSING_GRID in flags:
        return "grid_inputs_stale"
    if QualityFlag.MISSING_BATTERY in flags:
        return "battery_power_unavailable"
    if (
        QualityFlag.MISSING_EV in flags
        and policy.automatic_discharge_mode != AutomaticDischargeMode.OFF
        and (not policy.battery_may_feed_ev or not policy.discharge_during_ev_charging)
    ):
        return "ev_power_unavailable"
    return None


def _apply_hysteresis(
    command: BatteryCommand,
    measurements: LiveMeasurements,
    state: LiveControlState,
) -> tuple[BatteryCommand, CommandMode | None, int | None, int | None]:
    """Apply deterministic direction-change smoothing using explicit state."""
    now_ms = measurements.captured_at_ms
    measured_power_w = measurements.battery_discharge_w - measurements.battery_charge_w
    direction = state.direction
    block_until_ms = state.charge_block_until_ms
    last_charge_at_ms = state.last_charge_at_ms
    if direction is None:
        if measured_power_w > POWER_TOLERANCE_W:
            direction = CommandMode.OUTPUT
        elif measured_power_w < -POWER_TOLERANCE_W:
            direction = CommandMode.INPUT
            last_charge_at_ms = now_ms

    if command.mode == CommandMode.OUTPUT:
        if (
            abs(measured_power_w) <= POWER_TOLERANCE_W
            and command.power_w < POWER_START_W
        ):
            return (
                _replace_command(
                    command, CommandMode.IDLE, 0.0, "power_start_threshold"
                ),
                direction,
                block_until_ms,
                last_charge_at_ms,
            )
        return command, CommandMode.OUTPUT, None, last_charge_at_ms

    if command.mode != CommandMode.INPUT:
        return command, direction, block_until_ms, last_charge_at_ms

    if block_until_ms is None and direction == CommandMode.OUTPUT:
        recent_charge = (
            last_charge_at_ms is not None
            and now_ms - last_charge_at_ms <= CHARGE_RECENT_WINDOW_MS
        )
        delay_ms = CHARGE_RESTART_SLOW_MS if recent_charge else CHARGE_RESTART_FAST_MS
        block_until_ms = now_ms + delay_ms
    if block_until_ms is not None and now_ms < block_until_ms:
        return (
            _replace_command(command, CommandMode.IDLE, 0.0, "direction_hysteresis"),
            direction,
            block_until_ms,
            last_charge_at_ms,
        )
    if abs(measured_power_w) <= POWER_TOLERANCE_W and command.power_w < POWER_START_W:
        return (
            _replace_command(command, CommandMode.IDLE, 0.0, "power_start_threshold"),
            direction,
            block_until_ms,
            last_charge_at_ms,
        )
    return command, CommandMode.INPUT, None, now_ms


def _replace_command(
    command: BatteryCommand,
    mode: CommandMode,
    power_w: float,
    reason: str,
) -> BatteryCommand:
    return BatteryCommand(
        command_id=(
            f"{command.directive_id}:{command.created_at_ms}:"
            f"{mode.value}:{round(power_w)}"
        ),
        directive_id=command.directive_id,
        created_at_ms=command.created_at_ms,
        valid_until_ms=command.valid_until_ms,
        mode=mode,
        power_w=power_w,
        reason=reason,
    )

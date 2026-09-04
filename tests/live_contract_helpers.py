"""Factories and probes for tests at the live-control contract seam."""

from __future__ import annotations

from dataclasses import dataclass

from custom_components.battery_strategy.const import (
    DISCHARGE_LOAD,
    DISCHARGE_PRICE_SENSITIVE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    PV_CHARGING_ON,
)
from custom_components.battery_strategy.contracts import (
    AutomaticDischargeMode,
    BatteryCommand,
    CommandMode,
    DataQuality,
    LiveControlResult,
    LiveControlState,
    LiveDiagnostics,
    LiveMeasurements,
    LivePolicy,
    ManualControlMode,
    PlanLiveDirective,
    QualityFlag,
    SlotKey,
)
from custom_components.battery_strategy.models import StrategyOptions
from custom_components.battery_strategy.strategy import DeterministicLiveController


def measurements(
    grid_import_w=0.0,
    grid_export_w=0.0,
    pv_w=0.0,
    battery_power_w=0.0,
    ev_power_w=0.0,
    soc_pct=50.0,
    *,
    captured_at_ms=0,
    quality_flags: tuple[QualityFlag, ...] = (),
):
    return LiveMeasurements(
        captured_at_ms=captured_at_ms,
        grid_import_w=grid_import_w,
        grid_export_w=grid_export_w,
        pv_generation_w=pv_w,
        battery_charge_w=max(0.0, -battery_power_w),
        battery_discharge_w=max(0.0, battery_power_w),
        ev_charge_w=ev_power_w,
        soc_pct=soc_pct,
        quality=DataQuality(
            coverage=0.0 if quality_flags else 1.0,
            flags=quality_flags,
        ),
    )


def policy_from_options(options: StrategyOptions) -> LivePolicy:
    return LivePolicy(
        pv_to_ev_first=options.pv_to_ev_first,
        discharge_during_ev_charging=options.discharge_during_ev_charging,
        battery_may_feed_ev=options.battery_may_feed_ev,
        ev_active_threshold_w=options.ev_active_threshold_w,
        min_command_power_w=options.min_command_power_w,
        max_charge_power_w=options.max_charge_power_w,
        max_discharge_power_w=options.max_discharge_power_w,
        automatic_discharge_mode={
            DISCHARGE_LOAD: AutomaticDischargeMode.LOAD_FOLLOWING,
            DISCHARGE_PRICE_SENSITIVE: AutomaticDischargeMode.PRICE_SENSITIVE,
        }.get(options.discharge, AutomaticDischargeMode.OFF),
        manual_mode={
            MANUAL_CHARGE: ManualControlMode.CHARGE,
            MANUAL_DISCHARGE: ManualControlMode.DISCHARGE,
        }.get(options.manual_mode, ManualControlMode.OFF),
        manual_power_w=options.manual_power_w,
    )


def directive(
    options: StrategyOptions,
    *,
    start_ms=0,
    pv_charge_allowed=None,
    grid_charge_allowed=False,
    required_charge_power_w=0.0,
    required_charge_remaining_kwh=0.0,
    discharge_budget_remaining_kwh=0.0,
) -> PlanLiveDirective:
    pv_allowed = (
        options.pv_charging == PV_CHARGING_ON
        if pv_charge_allowed is None
        else pv_charge_allowed
    )
    return PlanLiveDirective(
        directive_id=f"test:{start_ms}",
        plan_id="test-plan",
        issued_at_ms=start_ms,
        slot=SlotKey(start_ms, start_ms + 900_000),
        pv_charge_allowed=pv_allowed,
        grid_charge_allowed=grid_charge_allowed,
        required_charge_power_w=required_charge_power_w,
        required_charge_remaining_kwh=required_charge_remaining_kwh,
        max_pv_charge_power_w=options.max_charge_power_w if pv_allowed else 0.0,
        max_grid_charge_power_w=(
            options.max_charge_power_w if grid_charge_allowed else 0.0
        ),
        max_discharge_power_w=options.max_discharge_power_w,
        discharge_budget_remaining_kwh=discharge_budget_remaining_kwh,
        min_soc_pct=options.min_soc_pct,
        max_soc_pct=options.max_soc_pct,
    )


def evaluate(
    inputs: LiveMeasurements,
    options: StrategyOptions,
    plan_directive: PlanLiveDirective | None = None,
) -> LiveControlResult:
    return DeterministicLiveController().command(
        plan_directive or directive(options),
        inputs,
        policy_from_options(options),
        LiveControlState(CommandMode.IDLE, 0.0, None),
    )


@dataclass(frozen=True)
class LiveDecisionProbe:
    """Test-only flattened assertion view; never crosses a production seam."""

    command: BatteryCommand
    diagnostics: LiveDiagnostics

    @property
    def mode(self):
        return self.command.mode.value

    @property
    def power_w(self):
        return self.command.power_w

    @property
    def reason(self):
        return self.command.reason

    def __getattr__(self, name):
        return getattr(self.diagnostics, name)


def probe(
    inputs: LiveMeasurements,
    options: StrategyOptions,
    plan_directive: PlanLiveDirective | None = None,
) -> LiveDecisionProbe:
    result = evaluate(inputs, options, plan_directive)
    return LiveDecisionProbe(result.command, result.diagnostics)

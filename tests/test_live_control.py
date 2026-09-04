"""Regression tests for the Zendure-style live controller."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.battery_strategy.actuator import (
    ActuationWriteTracker,
    HomeAssistantZendureActuator,
)
from custom_components.battery_strategy.const import (
    COMMAND_IDLE,
    COMMAND_INPUT,
    COMMAND_OUTPUT,
    DISCHARGE_LOAD,
)
from custom_components.battery_strategy.contracts import (
    BatteryCommand,
    CommandMode,
    LiveControlState,
    QualityFlag,
)
from custom_components.battery_strategy.live_control import P1UpdateGate
from custom_components.battery_strategy.models import StrategyOptions
from custom_components.battery_strategy.strategy import DeterministicLiveController
from tests.live_contract_helpers import (
    directive,
    measurements,
    policy_from_options,
    probe,
)


def live_decision(
    mode: str,
    power_w: int,
    measured_battery_w: float,
    now_ms: int,
    state: LiveControlState,
):
    options = StrategyOptions(
        manual_mode=(
            "charge"
            if mode == COMMAND_INPUT
            else "discharge"
            if mode == COMMAND_OUTPUT
            else "off"
        ),
        manual_power_w=power_w,
        discharge="off",
        pv_charging="off",
    )
    sample = measurements(battery_power_w=measured_battery_w, captured_at_ms=now_ms)
    return DeterministicLiveController().command(
        directive(options, start_ms=0, pv_charge_allowed=False),
        sample,
        policy_from_options(options),
        state,
    )


def actuator_command(mode: CommandMode, power_w: float, reason: str = "test"):
    """Return one valid command at the hardware boundary."""
    return BatteryCommand(
        command_id=f"test:{mode}:{power_w}:{reason}",
        directive_id="test-directive",
        created_at_ms=1,
        valid_until_ms=9_999_999_999_999,
        mode=mode,
        power_w=power_w,
        reason=reason,
    )


def test_p1_gate_copies_zendure_fast_and_normal_intervals():
    gate = P1UpdateGate()

    assert gate.should_refresh(0, 100.0)
    assert not gate.should_refresh(10, 101.0)
    assert not gate.should_refresh(10, 103.0)
    assert gate.should_refresh(10, 104.1)
    assert not gate.should_refresh(10, 105.0)
    assert gate.should_refresh(1000, 106.4)


def test_direction_change_uses_fast_charge_restart_after_long_discharge():
    state = LiveControlState(CommandMode.IDLE, 0.0, None)
    output = live_decision(COMMAND_OUTPUT, 500, 500, 0, state)
    assert output.command.mode == COMMAND_OUTPUT
    blocked = live_decision(COMMAND_INPUT, 500, 0, 10_000, output.state)
    assert blocked.command.mode == COMMAND_IDLE
    assert blocked.command.reason == "direction_hysteresis"
    still_blocked = live_decision(COMMAND_INPUT, 500, 0, 11_900, blocked.state)
    assert still_blocked.command.mode == COMMAND_IDLE
    allowed = live_decision(COMMAND_INPUT, 500, 0, 12_000, still_blocked.state)
    assert allowed.command.mode == COMMAND_INPUT


def test_recent_charge_uses_zendure_slow_restart_guard():
    state = LiveControlState(CommandMode.IDLE, 0.0, None)
    charged = live_decision(COMMAND_INPUT, 500, -500, 0, state)
    output = live_decision(COMMAND_OUTPUT, 500, 500, 10_000, charged.state)
    blocked = live_decision(COMMAND_INPUT, 500, 0, 20_000, output.state)
    assert blocked.command.mode == COMMAND_IDLE
    still_blocked = live_decision(COMMAND_INPUT, 500, 0, 79_900, blocked.state)
    assert still_blocked.command.mode == COMMAND_IDLE
    allowed = live_decision(COMMAND_INPUT, 500, 0, 80_000, still_blocked.state)
    assert allowed.command.mode == COMMAND_INPUT


def test_idle_device_uses_zendure_50_watt_start_threshold():
    state = LiveControlState(CommandMode.IDLE, 0.0, None)
    stopped = live_decision(COMMAND_OUTPUT, 49, 0, 0, state)
    assert stopped.command.mode == COMMAND_IDLE
    assert stopped.command.reason == "power_start_threshold"
    started = live_decision(COMMAND_OUTPUT, 50, 0, 1_000, stopped.state)
    assert started.command.mode == COMMAND_OUTPUT


def test_write_tracker_uses_own_write_time_and_confirms_or_retries():
    tracker = ActuationWriteTracker()
    assert tracker.should_write_limit("output", 500, 600, 0, 5)
    tracker.record("output", 600, 0)
    assert not tracker.should_write_limit("output", 500, 600, 4, 5)
    assert tracker.should_write_limit("output", 500, 600, 8, 5)
    assert not tracker.should_write_limit("output", 598, 600, 9, 5)
    assert tracker.should_write_limit("output", 600, 700, 10.3, 5)


def test_direction_change_stops_opposite_limit_before_mode_and_target():
    calls = []

    class Services:
        @staticmethod
        async def async_call(domain, service, data, blocking=False):
            calls.append((domain, service, data, blocking))

    states = {
        "ac_mode": SimpleNamespace(state="Input mode"),
        "input_limit": SimpleNamespace(state="400"),
        "output_limit": SimpleNamespace(state="0"),
    }
    hass = SimpleNamespace(services=Services(), states=SimpleNamespace(get=states.get))
    actuator = HomeAssistantZendureActuator(
        hass, "ac_mode", "input_limit", "output_limit"
    )

    asyncio.run(actuator.apply(actuator_command(CommandMode.OUTPUT, 600)))

    assert calls == [
        (
            "number",
            "set_value",
            {"entity_id": "input_limit", "value": 0},
            True,
        ),
        (
            "select",
            "select_option",
            {"entity_id": "ac_mode", "option": "Output mode"},
            True,
        ),
        (
            "number",
            "set_value",
            {"entity_id": "output_limit", "value": 600},
            True,
        ),
    ]


def test_actuator_failsafe_and_disabled_zero_write_semantics():
    calls = []

    class Services:
        @staticmethod
        async def async_call(domain, service, data, blocking=False):
            calls.append((domain, service, data, blocking))

    states = {
        "ac_mode": SimpleNamespace(state="Output mode"),
        "input_limit": SimpleNamespace(state="0"),
        "output_limit": SimpleNamespace(state="300"),
    }
    actuator = HomeAssistantZendureActuator(
        SimpleNamespace(services=Services(), states=SimpleNamespace(get=states.get)),
        "ac_mode",
        "input_limit",
        "output_limit",
    )

    async def scenario():
        first = await actuator.apply(
            actuator_command(CommandMode.IDLE, 0, "grid_inputs_stale")
        )
        second = await actuator.apply(
            actuator_command(CommandMode.IDLE, 0, "grid_inputs_stale")
        )
        disabled = await actuator.apply(
            actuator_command(CommandMode.IDLE, 0, "strategy_disabled")
        )
        return first, second, disabled

    first, second, disabled = asyncio.run(scenario())
    assert first.applied
    assert first.detail.startswith("written:")
    assert second.detail == "failsafe_pending_confirmation"
    assert disabled.applied
    assert [call[2] for call in calls] == [
        {"entity_id": "output_limit", "value": 0},
        {"entity_id": "input_limit", "value": 0},
        {"entity_id": "output_limit", "value": 0},
    ]


def test_unconfirmed_failsafe_zero_is_retried_after_confirmation_timeout():
    calls = []

    class Services:
        @staticmethod
        async def async_call(domain, service, data, blocking=False):
            calls.append(data)

    states = {
        "ac_mode": SimpleNamespace(state="Output mode"),
        "input_limit": SimpleNamespace(state="0"),
        "output_limit": SimpleNamespace(state="300"),
    }
    actuator = HomeAssistantZendureActuator(
        SimpleNamespace(services=Services(), states=SimpleNamespace(get=states.get)),
        "ac_mode",
        "input_limit",
        "output_limit",
    )

    async def scenario():
        with patch(
            "custom_components.battery_strategy.actuator.time.monotonic",
            side_effect=(0.0, 9.0),
        ):
            await actuator.apply(
                actuator_command(CommandMode.IDLE, 0, "grid_inputs_stale")
            )
            await actuator.apply(
                actuator_command(CommandMode.IDLE, 0, "grid_inputs_stale")
            )

    asyncio.run(scenario())
    assert calls == [
        {"entity_id": "output_limit", "value": 0},
        {"entity_id": "output_limit", "value": 0},
    ]


def test_ev_dashboard_switch_matrix_remains_authoritative():
    inputs = measurements(5000, 0, 0, 0, 4200, 80)

    def decision(*, discharge_during_ev_charging, battery_may_feed_ev):
        return probe(
            inputs,
            StrategyOptions(
                discharge=DISCHARGE_LOAD,
                discharge_during_ev_charging=discharge_during_ev_charging,
                battery_may_feed_ev=battery_may_feed_ev,
            ),
        )

    both_off = decision(discharge_during_ev_charging=False, battery_may_feed_ev=False)
    house_only = decision(discharge_during_ev_charging=True, battery_may_feed_ev=False)
    including_ev = decision(discharge_during_ev_charging=True, battery_may_feed_ev=True)
    global_block = decision(
        discharge_during_ev_charging=False, battery_may_feed_ev=True
    )

    assert (both_off.mode, both_off.power_w) == (COMMAND_IDLE, 0)
    assert (house_only.mode, house_only.power_w) == (COMMAND_OUTPUT, 800)
    assert (including_ev.mode, including_ev.power_w) == (COMMAND_OUTPUT, 2400)
    assert (global_block.mode, global_block.power_w) == (COMMAND_IDLE, 0)


def test_idle_decision_preserves_last_direction_state():
    state = LiveControlState(CommandMode.OUTPUT, 800, 0, direction=CommandMode.OUTPUT)
    stopped = live_decision(COMMAND_IDLE, 0, 800, 1_000, state)
    assert stopped.command.mode == COMMAND_IDLE
    assert stopped.command.power_w == 0
    assert stopped.state.direction == CommandMode.OUTPUT


def test_expired_directive_stops_automatic_control_with_actuator_validity():
    options = StrategyOptions(discharge=DISCHARGE_LOAD)
    sample = measurements(800, captured_at_ms=900_001)

    result = DeterministicLiveController().command(
        directive(options, start_ms=0),
        sample,
        policy_from_options(options),
        LiveControlState(CommandMode.OUTPUT, 800, 0),
    )

    assert result.command.mode == CommandMode.IDLE
    assert result.command.reason == "directive_outside_slot"
    assert result.command.valid_until_ms == sample.captured_at_ms + 30_000


def test_manual_control_remains_available_when_plan_directive_expired():
    options = StrategyOptions(
        manual_mode="discharge", manual_power_w=700, discharge="off"
    )
    sample = measurements(soc_pct=50, captured_at_ms=900_001)

    result = DeterministicLiveController().command(
        directive(options, start_ms=0),
        sample,
        policy_from_options(options),
        LiveControlState(CommandMode.IDLE, 0, None),
    )

    assert result.command.mode == CommandMode.OUTPUT
    assert result.command.power_w == 700
    assert result.command.reason == "manual_discharge"
    assert result.command.valid_until_ms == sample.captured_at_ms + 30_000


def test_missing_battery_measurement_fails_closed_before_automatic_control():
    options = StrategyOptions(discharge=DISCHARGE_LOAD)
    sample = measurements(
        800,
        captured_at_ms=1,
        quality_flags=(QualityFlag.MISSING_BATTERY,),
    )

    result = DeterministicLiveController().command(
        directive(options),
        sample,
        policy_from_options(options),
        LiveControlState(CommandMode.IDLE, 0, None),
    )

    assert result.command.mode == CommandMode.IDLE
    assert result.command.reason == "battery_power_unavailable"

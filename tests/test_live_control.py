"""Regression tests for the Zendure-style live controller."""

import asyncio
from types import SimpleNamespace

from custom_components.battery_strategy.actuator import ActuationWriteTracker
from custom_components.battery_strategy.const import (
    COMMAND_IDLE,
    COMMAND_INPUT,
    COMMAND_OUTPUT,
    DISCHARGE_LOAD,
)
from custom_components.battery_strategy.coordinator import BatteryStrategyCoordinator
from custom_components.battery_strategy.live_control import (
    DirectionHysteresis,
    P1UpdateGate,
)
from custom_components.battery_strategy.models import (
    StrategyCommand,
    StrategyInputs,
    StrategyOptions,
)
from custom_components.battery_strategy.plan_models import PlanLiveDirective
from custom_components.battery_strategy.strategy import (
    calculate_command,
    live_command_from_directive,
)


def command(mode: str, power_w: int) -> StrategyCommand:
    """Return a minimal command for live-controller tests."""
    return StrategyCommand(mode, power_w, "test", 0, 0, 0, 0, 0, 0)


def test_p1_gate_copies_zendure_fast_and_normal_intervals():
    gate = P1UpdateGate()

    assert gate.should_refresh(0, 100.0)
    assert not gate.should_refresh(10, 101.0)
    assert not gate.should_refresh(10, 103.0)
    assert gate.should_refresh(10, 104.1)
    assert not gate.should_refresh(10, 105.0)
    assert gate.should_refresh(1000, 106.4)


def test_direction_change_uses_fast_charge_restart_after_long_discharge():
    guard = DirectionHysteresis()

    assert guard.apply(command(COMMAND_OUTPUT, 500), 500, 0).mode == COMMAND_OUTPUT
    blocked = guard.apply(command(COMMAND_INPUT, 500), 0, 10)
    assert blocked.mode == COMMAND_IDLE
    assert blocked.reason == "direction_hysteresis"
    assert guard.apply(command(COMMAND_INPUT, 500), 0, 11.9).mode == COMMAND_IDLE
    assert guard.apply(command(COMMAND_INPUT, 500), 0, 12.0).mode == COMMAND_INPUT


def test_recent_charge_uses_zendure_slow_restart_guard():
    guard = DirectionHysteresis()

    assert guard.apply(command(COMMAND_INPUT, 500), -500, 0).mode == COMMAND_INPUT
    assert guard.apply(command(COMMAND_OUTPUT, 500), 500, 10).mode == COMMAND_OUTPUT
    assert guard.apply(command(COMMAND_INPUT, 500), 0, 20).mode == COMMAND_IDLE
    assert guard.apply(command(COMMAND_INPUT, 500), 0, 79.9).mode == COMMAND_IDLE
    assert guard.apply(command(COMMAND_INPUT, 500), 0, 80).mode == COMMAND_INPUT


def test_idle_device_uses_zendure_50_watt_start_threshold():
    guard = DirectionHysteresis()

    stopped = guard.apply(command(COMMAND_OUTPUT, 49), 0, 0)
    assert stopped.mode == COMMAND_IDLE
    assert stopped.reason == "power_start_threshold"
    assert guard.apply(command(COMMAND_OUTPUT, 50), 0, 1).mode == COMMAND_OUTPUT


def test_write_tracker_uses_own_write_time_and_confirms_or_retries():
    tracker = ActuationWriteTracker()
    options = StrategyOptions(min_command_delta_w=5)

    assert tracker.should_write_limit("output", 500, 600, 0, options)
    tracker.record("output", 600, 0)
    assert not tracker.should_write_limit("output", 500, 600, 4, options)
    assert tracker.should_write_limit("output", 500, 600, 8, options)
    assert not tracker.should_write_limit("output", 598, 600, 9, options)
    assert tracker.should_write_limit("output", 600, 700, 10.3, options)


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
    coordinator = object.__new__(BatteryStrategyCoordinator)
    coordinator.hass = SimpleNamespace(
        services=Services(), states=SimpleNamespace(get=states.get)
    )
    coordinator.entry = SimpleNamespace(data={})
    coordinator._entity_id = lambda key: {
        "zendure_ac_mode_entity": "ac_mode",
        "zendure_input_limit_entity": "input_limit",
        "zendure_output_limit_entity": "output_limit",
    }[key]
    coordinator._state_available = lambda entity: entity in states
    coordinator._raw_state_float = lambda entity: float(states[entity].state)
    coordinator._grid_inputs_fresh = lambda: True
    coordinator._soc_control_ready = True
    coordinator._ev_control_ready = True
    coordinator._failsafe_zeroed_reason = None
    coordinator._write_tracker = ActuationWriteTracker()

    asyncio.run(
        coordinator._async_apply_command(
            command(COMMAND_OUTPUT, 600), StrategyOptions()
        )
    )

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


def test_ev_dashboard_switch_matrix_remains_authoritative():
    inputs = StrategyInputs(
        grid_import_w=5000,
        grid_export_w=0,
        pv_w=0,
        battery_power_w=0,
        ev_power_w=4200,
        soc_pct=80,
    )

    both_off = calculate_command(
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=False,
            battery_may_feed_ev=False,
        ),
    )
    discharge_house_only = calculate_command(
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=True,
            battery_may_feed_ev=False,
        ),
    )
    discharge_including_ev = calculate_command(
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=True,
            battery_may_feed_ev=True,
        ),
    )
    global_block_wins = calculate_command(
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=False,
            battery_may_feed_ev=True,
        ),
    )

    directive = PlanLiveDirective(
        slot_id="test",
        slot_start_ts=0,
        slot_end_ts=900_000,
        pv_charge_allowed=True,
        must_charge_w=0,
        must_charge_remaining_kwh=0,
        grid_charge_allowed=False,
        discharge_budget_kwh=1,
        battery_min_soc_pct=10,
        battery_max_soc_pct=100,
    )
    both_off_live = live_command_from_directive(
        directive,
        both_off,
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=False,
            battery_may_feed_ev=False,
        ),
    )
    discharge_house_only_live = live_command_from_directive(
        directive,
        discharge_house_only,
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=True,
            battery_may_feed_ev=False,
        ),
    )
    discharge_including_ev_live = live_command_from_directive(
        directive,
        discharge_including_ev,
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=True,
            battery_may_feed_ev=True,
        ),
    )
    global_block_wins_live = live_command_from_directive(
        directive,
        global_block_wins,
        inputs,
        StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=False,
            battery_may_feed_ev=True,
        ),
    )

    assert (both_off.mode, both_off.power_w) == (COMMAND_IDLE, 0)
    assert (discharge_house_only.mode, discharge_house_only.power_w) == (
        COMMAND_OUTPUT,
        800,
    )
    assert (discharge_including_ev.mode, discharge_including_ev.power_w) == (
        COMMAND_OUTPUT,
        2400,
    )
    assert (global_block_wins.mode, global_block_wins.power_w) == (COMMAND_IDLE, 0)
    assert (both_off_live.mode, both_off_live.power_w) == (COMMAND_IDLE, 0)
    assert (
        discharge_house_only_live.mode,
        discharge_house_only_live.power_w,
    ) == (COMMAND_OUTPUT, 800)
    assert (
        discharge_including_ev_live.mode,
        discharge_including_ev_live.power_w,
    ) == (COMMAND_OUTPUT, 2400)
    assert (global_block_wins_live.mode, global_block_wins_live.power_w) == (
        COMMAND_IDLE,
        0,
    )


def test_ev_idle_decision_passes_direction_guard_unchanged():
    guard = DirectionHysteresis()
    guard.apply(command(COMMAND_OUTPUT, 800), 800, 0)

    stopped = guard.apply(command(COMMAND_IDLE, 0), 800, 1)

    assert stopped.mode == COMMAND_IDLE
    assert stopped.power_w == 0

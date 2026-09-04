"""Regression tests for HA runtime ownership and in-slot restart safety."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.battery_strategy import async_setup
from custom_components.battery_strategy.compiler_runtime import PlanCompilerRuntime
from custom_components.battery_strategy.compiler_runtime_store import (
    CompilerRuntimeSnapshot,
)
from custom_components.battery_strategy.const import (
    COMMAND_OUTPUT,
    DISCHARGE_PRICE_SENSITIVE,
    GRID_CHARGING_PRICE_SENSITIVE,
    PV_CHARGING_ON,
)
from custom_components.battery_strategy.contracts import (
    ActuationResult,
    PlanCompilationState,
    SlotKey,
)
from custom_components.battery_strategy.coordinator import BatteryStrategyCoordinator
from custom_components.battery_strategy.models import StrategyInputs, StrategyOptions
from custom_components.battery_strategy.plan_models import (
    PlanLiveDirective,
    PlanPoint,
    StrategyPlan,
)

SLOT_START_MS = 1_800_000_000_000
SLOT = SlotKey(SLOT_START_MS, SLOT_START_MS + 900_000)


def test_domain_services_register_once_across_entry_reload_cycles():
    class Services:
        def __init__(self):
            self.registered = {}

        def has_service(self, domain, service):
            return (domain, service) in self.registered

        def async_register(self, domain, service, handler):
            self.registered[(domain, service)] = handler

    services = Services()
    hass = SimpleNamespace(services=services)

    assert asyncio.run(async_setup(hass, {}))
    assert asyncio.run(async_setup(hass, {}))
    assert len(services.registered) == 4


def _snapshot(
    *,
    clean: bool,
    charged_kwh: float = 0.0,
    discharged_kwh: float = 0.1,
    input_energy_kwh: float | None = None,
    output_energy_kwh: float | None = None,
) -> CompilerRuntimeSnapshot:
    return CompilerRuntimeSnapshot(
        saved_at_ms=SLOT_START_MS + 60_000,
        compilation_state=PlanCompilationState(
            slot=SLOT,
            committed_plan_id="plan-before-restart",
            discharge_budget_commitment_kwh=0.6,
        ),
        charged_kwh=charged_kwh,
        discharged_kwh=discharged_kwh,
        input_energy_kwh=input_energy_kwh,
        output_energy_kwh=output_energy_kwh,
        clean_shutdown=clean,
    )


def _options() -> StrategyOptions:
    return StrategyOptions(
        pv_charging=PV_CHARGING_ON,
        grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
        discharge=DISCHARGE_PRICE_SENSITIVE,
        min_soc_pct=10,
        max_soc_pct=100,
        battery_capacity_kwh=6,
        max_charge_power_w=2400,
        max_discharge_power_w=2400,
    )


def _plan(discharge_budget_kwh: float = 0.6) -> StrategyPlan:
    return StrategyPlan(
        points=[
            PlanPoint(
                ts_ms=SLOT_START_MS,
                date="2027-01-15",
                price_ct=40,
                load_fc_w=1000,
                pv_fc_w=0,
                grid_import_fc_w=1000,
                grid_export_fc_w=0,
                grid_net_fc_w=1000,
                mode="output",
                power_w=1000,
                charge_fc_w=0,
                discharge_fc_w=1000,
                soc_pct=50,
                discharge_budget_kwh=discharge_budget_kwh,
            )
        ],
        current_mode="output",
        current_power_w=1000,
        reason="test",
    )


def test_compiler_snapshot_round_trip_is_strict_and_lossless():
    snapshot = _snapshot(
        clean=False,
        charged_kwh=0.2,
        discharged_kwh=0.3,
        input_energy_kwh=12.4,
        output_energy_kwh=8.7,
    )

    assert (
        CompilerRuntimeSnapshot.from_storage_dict(snapshot.as_storage_dict())
        == snapshot
    )
    assert CompilerRuntimeSnapshot.from_storage_dict({"slot_start_ms": 0}) is None


def test_clean_reload_restores_latched_budget_without_energy_counters():
    runtime = PlanCompilerRuntime(_snapshot(clean=True, discharged_kwh=0.2))

    runtime.sync_slot(SLOT_START_MS, SLOT_START_MS + 120_000, (None, None))

    assert runtime.progress_reconstructable
    assert runtime.discharged_kwh == pytest.approx(0.2)
    directive = runtime.compile(
        _plan(),
        _options(),
        StrategyInputs(1000, 0, 0, 0, soc_pct=50),
        SLOT_START_MS + 120_000,
    )
    assert directive.discharge_budget_kwh == pytest.approx(0.4)


def test_unclean_restart_reconstructs_progress_from_monotonic_counters():
    runtime = PlanCompilerRuntime(
        _snapshot(
            clean=False,
            charged_kwh=0.05,
            discharged_kwh=0.1,
            input_energy_kwh=10.0,
            output_energy_kwh=20.0,
        )
    )

    runtime.sync_slot(SLOT_START_MS, SLOT_START_MS + 180_000, (10.2, 20.15))

    assert runtime.progress_reconstructable
    assert runtime.charged_kwh == pytest.approx(0.25)
    assert runtime.discharged_kwh == pytest.approx(0.25)


def test_unclean_restart_without_counters_fails_commercially_closed():
    runtime = PlanCompilerRuntime(_snapshot(clean=False))

    runtime.sync_slot(SLOT_START_MS, SLOT_START_MS + 180_000, (None, None))
    directive = runtime.compile(
        _plan(),
        _options(),
        StrategyInputs(1000, 0, 0, 0, soc_pct=50),
        SLOT_START_MS + 180_000,
    )

    assert not runtime.progress_reconstructable
    assert runtime.error == "slot_progress_unrecoverable"
    assert directive.pv_charge_allowed
    assert not directive.grid_charge_allowed
    assert directive.discharge_budget_kwh == 0.0


def test_running_process_resets_progress_only_at_next_slot():
    runtime = PlanCompilerRuntime()
    start = dt.datetime.fromtimestamp(SLOT_START_MS / 1000, dt.timezone.utc)
    runtime.sync_slot(SLOT_START_MS, SLOT_START_MS, (None, None))
    runtime.account(start, -1200.0)
    runtime.account(start + dt.timedelta(minutes=10), 1800.0)
    runtime.account(start + dt.timedelta(minutes=20), 0.0)

    next_start = SLOT.end_ms
    runtime.sync_slot(next_start, next_start + 1_000, (None, None))

    assert runtime.progress_reconstructable
    assert runtime.charged_kwh == 0.0
    assert runtime.discharged_kwh == 0.0
    assert runtime.compilation_state == PlanCompilationState()


def test_optimizer_prefetch_and_expiry_are_each_requested_once():
    runtime = PlanCompilerRuntime()
    runtime.sync_slot(SLOT_START_MS, SLOT_START_MS, (None, None))
    coordinator = object.__new__(BatteryStrategyCoordinator)
    coordinator._compiler_runtime = runtime
    coordinator._last_optimizer_force_key = None

    def at(timestamp_ms: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(timestamp_ms / 1000, dt.timezone.utc)

    assert not coordinator._should_force_optimizer(at(SLOT.end_ms - 60_001))
    assert coordinator._should_force_optimizer(at(SLOT.end_ms - 60_000))
    assert not coordinator._should_force_optimizer(at(SLOT.end_ms - 30_000))
    assert coordinator._should_force_optimizer(at(SLOT.end_ms))
    assert not coordinator._should_force_optimizer(at(SLOT.end_ms + 1_000))


def test_coordinator_cycle_preserves_runtime_order_and_compiled_permission():
    calls: list[str] = []
    now = dt.datetime.now(dt.timezone.utc)
    slot_start_ms = int(now.timestamp() * 1000) // 900_000 * 900_000
    options = _options()
    inputs = StrategyInputs(500.0, 0.0, 0.0, 0.0, soc_pct=50.0)
    plan = StrategyPlan(
        points=[
            PlanPoint(
                ts_ms=slot_start_ms,
                date=now.date().isoformat(),
                price_ct=40.0,
                load_fc_w=500,
                pv_fc_w=0,
                grid_import_fc_w=500,
                grid_export_fc_w=0,
                grid_net_fc_w=500,
                mode=COMMAND_OUTPUT,
                power_w=500,
                charge_fc_w=0,
                discharge_fc_w=500,
                soc_pct=50.0,
                discharge_budget_kwh=0.125,
            )
        ],
        current_mode=COMMAND_OUTPUT,
        current_power_w=500,
        reason="cycle-test",
    )
    directive = PlanLiveDirective(
        slot_id=str(slot_start_ms),
        slot_start_ts=slot_start_ms,
        slot_end_ts=slot_start_ms + 900_000,
        pv_charge_allowed=True,
        must_charge_w=0,
        must_charge_remaining_kwh=0.0,
        grid_charge_allowed=False,
        discharge_budget_kwh=0.125,
        battery_min_soc_pct=10.0,
        battery_max_soc_pct=100.0,
    )

    class CompilerRuntime:
        snapshot_dirty = True
        error = None
        active_slot_end_ms = 0
        active_slot_id = None

        @staticmethod
        def account(_now, battery_power_w):
            assert battery_power_w == 0.0
            calls.append("account")

        @staticmethod
        def sync_slot(start_ms, _now_ms, energy_totals):
            assert start_ms == slot_start_ms
            assert energy_totals == (None, None)
            calls.append("sync")

        @staticmethod
        def compile(compiled_plan, compiled_options, compiled_inputs, _now_ms):
            assert compiled_plan is plan
            assert compiled_options is options
            assert compiled_inputs is inputs
            calls.append("compile")
            return directive

    class PlanningAdapter:
        @staticmethod
        def set_forecast_environment(*_args):
            return None

        @staticmethod
        def runtime_context(runtime_inputs, runtime_options):
            assert runtime_inputs is inputs
            assert runtime_options is options
            return {}

        @staticmethod
        def age_s():
            return 0.0

    class Planner:
        running = False
        last_error = None

        @staticmethod
        def maybe_schedule(*_args, **_kwargs):
            calls.append("schedule")
            return True

        @staticmethod
        def current(*_args):
            return plan, {}

    components = SimpleNamespace(powers_w={}, features={}, drivers=(), specs=())
    coordinator = object.__new__(BatteryStrategyCoordinator)
    coordinator.entry = SimpleNamespace(
        data={}, options={"strategy_enabled": False, "trace_enabled": False}
    )
    coordinator.hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="UTC", path=lambda value: value)
    )
    coordinator._manual_until = None
    coordinator._weather = ()
    coordinator._weather_error = None
    coordinator._feature_history = ()
    coordinator._planning_pipeline = PlanningAdapter()
    coordinator._planner = Planner()
    coordinator._compiler_runtime = CompilerRuntime()
    coordinator._last_optimizer_force_key = None
    coordinator._soc_control_ready = True
    coordinator._soc_recovered = False
    coordinator._optimizer_attrs = {}
    coordinator._feature_aggregator = SimpleNamespace(
        observe=lambda _observation: (), active_coverage=1.0
    )
    coordinator._feature_store = SimpleNamespace(
        diagnostics=lambda _coverage: {}, last_error=None
    )
    coordinator._strategy_options = lambda: options
    coordinator._strategy_inputs = lambda: inputs
    coordinator._schedule_weather_refresh = lambda *_args: None
    coordinator._current_price_ct = lambda _now: 40.0
    coordinator._feature_quality_flags = lambda: ()
    coordinator._battery_energy_totals = lambda: (None, None)
    coordinator._disabled_zeroed = True
    coordinator._strategy_was_enabled = False
    coordinator.last_actuation = ActuationResult("test", False, 0, "not_started")

    async def persist(*, clean_shutdown):
        assert not clean_shutdown
        calls.append("persist")

    coordinator._async_persist_compiler_runtime = persist
    with (
        patch(
            "custom_components.battery_strategy.coordinator.collect_load_components",
            return_value=components,
        ),
        patch(
            "custom_components.battery_strategy.coordinator.add_central_weather",
            return_value=components,
        ),
        patch(
            "custom_components.battery_strategy.coordinator.build_operator_projection",
            return_value={},
        ),
    ):
        data = asyncio.run(coordinator._async_update_data())

    assert calls == ["account", "schedule", "sync", "compile", "persist"]
    assert data["plan_to_live"] is directive
    assert data["calculated_command"].mode == COMMAND_OUTPUT
    assert data["calculated_command"].power_w == 500

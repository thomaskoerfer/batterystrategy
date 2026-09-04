"""Regression tests for HA runtime ownership and in-slot restart safety."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.battery_strategy import async_setup
from custom_components.battery_strategy.compiler_runtime_store import (
    CompilerRuntimeSnapshot,
)
from custom_components.battery_strategy.const import (
    CONF_BATTERY_INPUT_ENERGY_ENTITY,
    CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
    DISCHARGE_PRICE_SENSITIVE,
    GRID_CHARGING_PRICE_SENSITIVE,
    PV_CHARGING_ON,
)
from custom_components.battery_strategy.contracts import (
    PlanCompilationState,
    SlotKey,
)
from custom_components.battery_strategy.coordinator import BatteryStrategyCoordinator
from custom_components.battery_strategy.models import StrategyInputs, StrategyOptions
from custom_components.battery_strategy.plan_compiler import DeterministicPlanCompiler
from custom_components.battery_strategy.plan_models import PlanPoint, StrategyPlan

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


def _coordinator(snapshot, states=None):
    coordinator = object.__new__(BatteryStrategyCoordinator)
    coordinator.entry = SimpleNamespace(
        data={
            CONF_BATTERY_INPUT_ENERGY_ENTITY: "sensor.battery_input",
            CONF_BATTERY_OUTPUT_ENERGY_ENTITY: "sensor.battery_output",
        }
    )
    state_map = {
        entity_id: SimpleNamespace(state=str(value))
        for entity_id, value in (states or {}).items()
    }
    coordinator.hass = SimpleNamespace(states=SimpleNamespace(get=state_map.get))
    coordinator._active_directive_slot_id = None
    coordinator._active_directive_slot_end_ts_ms = 0
    coordinator._slot_charged_kwh = 0.0
    coordinator._slot_discharged_kwh = 0.0
    coordinator._plan_compiler = DeterministicPlanCompiler()
    coordinator._plan_compilation_state = PlanCompilationState()
    coordinator._restored_compiler_runtime = snapshot
    coordinator._compiler_progress_reconstructable = True
    coordinator._compiler_snapshot_dirty = False
    coordinator._plan_compiler_error = None
    return coordinator


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
    coordinator = _coordinator(_snapshot(clean=True, discharged_kwh=0.2))

    coordinator._sync_slot_progress(SLOT_START_MS, SLOT_START_MS + 120_000)

    assert coordinator._compiler_progress_reconstructable
    assert coordinator._slot_discharged_kwh == pytest.approx(0.2)
    directive = coordinator._compile_authoritative_directive(
        _plan(),
        _options(),
        StrategyInputs(1000, 0, 0, 0, soc_pct=50),
        SLOT_START_MS + 120_000,
    )
    assert directive.discharge_budget_kwh == pytest.approx(0.4)


def test_unclean_restart_reconstructs_progress_from_monotonic_counters():
    coordinator = _coordinator(
        _snapshot(
            clean=False,
            charged_kwh=0.05,
            discharged_kwh=0.1,
            input_energy_kwh=10.0,
            output_energy_kwh=20.0,
        ),
        {"sensor.battery_input": 10.2, "sensor.battery_output": 20.15},
    )

    coordinator._sync_slot_progress(SLOT_START_MS, SLOT_START_MS + 180_000)

    assert coordinator._compiler_progress_reconstructable
    assert coordinator._slot_charged_kwh == pytest.approx(0.25)
    assert coordinator._slot_discharged_kwh == pytest.approx(0.25)


def test_unclean_restart_without_counters_fails_commercially_closed():
    coordinator = _coordinator(_snapshot(clean=False))

    coordinator._sync_slot_progress(SLOT_START_MS, SLOT_START_MS + 180_000)
    directive = coordinator._compile_authoritative_directive(
        _plan(),
        _options(),
        StrategyInputs(1000, 0, 0, 0, soc_pct=50),
        SLOT_START_MS + 180_000,
    )

    assert not coordinator._compiler_progress_reconstructable
    assert coordinator._plan_compiler_error == "slot_progress_unrecoverable"
    assert directive.pv_charge_allowed
    assert not directive.grid_charge_allowed
    assert directive.discharge_budget_kwh == 0.0


def test_running_process_resets_progress_only_at_next_slot():
    coordinator = _coordinator(None)
    coordinator._active_directive_slot_id = str(SLOT_START_MS)
    coordinator._slot_charged_kwh = 0.2
    coordinator._slot_discharged_kwh = 0.3

    next_start = SLOT.end_ms
    coordinator._sync_slot_progress(next_start, next_start + 1_000)

    assert coordinator._compiler_progress_reconstructable
    assert coordinator._slot_charged_kwh == 0.0
    assert coordinator._slot_discharged_kwh == 0.0
    assert coordinator._plan_compilation_state == PlanCompilationState()

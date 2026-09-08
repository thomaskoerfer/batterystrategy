from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest

from custom_components.battery_strategy.component_history import (
    backfill_component_power_history,
    merge_feature_history,
)
from custom_components.battery_strategy.component_history_adapter import (
    _await_executor_completion,
    async_backfill_cyclic_component_history,
)
from custom_components.battery_strategy.const import (
    CONF_COMPONENT_KEY,
    CONF_COMPONENT_POWER_ENTITY,
    CONF_LOAD_COMPONENT_PROFILE,
    LOAD_PROFILE_CYCLIC_APPLIANCE,
    SUBENTRY_TYPE_LOAD_COMPONENT,
)
from custom_components.battery_strategy.contracts import (
    DataQuality,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    QualityFlag,
    SlotKey,
)
from custom_components.battery_strategy.coordinator import BatteryStrategyCoordinator

SLOT_MS = 15 * 60 * 1000


def _slot(start_ms: int, components=()) -> HistoricalFeatureSlot:
    return HistoricalFeatureSlot(
        SlotKey(start_ms, start_ms + SLOT_MS),
        house_load_no_ev_kwh=0.5,
        pv_generation_kwh=0.0,
        grid_import_kwh=0.5,
        grid_export_kwh=0.0,
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        ev_charge_kwh=0.0,
        price_ct_per_kwh=30.0,
        quality=DataQuality(),
        load_components=tuple(components),
    )


def test_backfill_integrates_stepwise_recorder_power_into_slots():
    history = (_slot(0), _slot(SLOT_MS))
    series = {
        "dryer": (
            (0.0, 0.0),
            (300.0, 1_000.0),
            (420.0, 1_000.0),
            (540.0, 1_000.0),
            (660.0, 1_000.0),
            (780.0, 1_000.0),
            (900.0, 1_000.0),
            (1_020.0, 1_000.0),
            (1_140.0, 1_000.0),
            (1_200.0, 0.0),
        )
    }

    changed = backfill_component_power_history(history, series)

    assert len(changed) == 2
    assert changed[0].load_components[0].component_key == "dryer"
    assert changed[0].load_components[0].energy_kwh == 0.16666666666666666
    assert changed[1].load_components[0].energy_kwh == 0.08333333333333333
    assert changed[0].load_components[0].quality.coverage == 1.0


def test_backfill_preserves_an_existing_component_observation():
    existing = LoadComponentEnergy("dryer", 0.4, DataQuality())
    history = (_slot(0, (existing,)),)

    changed = backfill_component_power_history(history, {"dryer": ((0.0, 0.0),)})

    assert changed == ()


def test_backfill_requires_a_state_at_or_before_slot_start():
    history = (_slot(0),)

    changed = backfill_component_power_history(history, {"dryer": ((60.0, 1_000.0),)})

    assert changed == ()


def test_backfill_marks_component_sum_above_house_load():
    history = (_slot(0),)

    changed = backfill_component_power_history(
        history,
        {"dryer": tuple((float(offset), 3_000.0) for offset in range(0, 900, 120))},
    )

    assert QualityFlag.COMPONENT_MISMATCH in changed[0].load_components[0].quality.flags


def test_setup_backfill_reads_recorder_and_persists_missing_component(
    monkeypatch,
):
    start_ms = int(time.time() * 1000) // SLOT_MS * SLOT_MS - SLOT_MS
    history = (_slot(start_ms),)
    data = {
        CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_CYCLIC_APPLIANCE,
        CONF_COMPONENT_KEY: "washer",
        CONF_COMPONENT_POWER_ENTITY: "sensor.power",
    }
    entry = SimpleNamespace(
        subentries={
            "washer": SimpleNamespace(
                subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT,
                data=data,
            )
        }
    )

    class _Hass:
        states = SimpleNamespace(
            get=lambda _entity_id: SimpleNamespace(
                attributes={"unit_of_measurement": "kW"}
            )
        )

        async def async_add_executor_job(self, target, *args):
            return target(*args)

    class _Store:
        def __init__(self):
            self.slots = history

        async def add_missing_load_components(self, slots):
            self.slots = merge_feature_history(self.slots, slots)
            return self.slots

    def _read(_hass, entity_map, scales, **_kwargs):
        assert entity_map == {"washer": "sensor.power"}
        assert scales == {"washer": 1_000.0}
        start_s = start_ms / 1000.0
        return {
            "washer": tuple((start_s + offset, 400.0) for offset in range(0, 900, 120))
        }

    monkeypatch.setattr(
        "custom_components.battery_strategy.component_history_adapter.read_recorder_series_with_availability",
        _read,
    )
    store = _Store()
    marker = SimpleNamespace(
        async_load=lambda: _async_value(None),
        async_save=lambda value: _async_value(value),
    )

    result = asyncio.run(
        async_backfill_cyclic_component_history(
            _Hass(),
            entry,
            store,
            history,
            as_of=dt.datetime.now(dt.UTC),
            marker_store=marker,
        )
    )

    assert result[0].load_components[0].component_key == "washer"
    assert result[0].load_components[0].energy_kwh == 0.1


def test_setup_backfill_does_not_rescan_after_completed_marker():
    start_ms = int(time.time() * 1000) // SLOT_MS * SLOT_MS - 2 * SLOT_MS
    existing = LoadComponentEnergy("washer", 0.0, DataQuality())
    history = (_slot(start_ms, (existing,)), _slot(start_ms + SLOT_MS))
    entry = SimpleNamespace(
        subentries={
            "washer": SimpleNamespace(
                subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT,
                data={
                    CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_CYCLIC_APPLIANCE,
                    CONF_COMPONENT_KEY: "washer",
                    CONF_COMPONENT_POWER_ENTITY: "sensor.power",
                },
            )
        }
    )
    hass = SimpleNamespace()
    store = SimpleNamespace()
    marker = SimpleNamespace(
        async_load=lambda: _async_value(
            {
                "completed": {
                    "washer": sha256(b"sensor.power").hexdigest(),
                }
            }
        )
    )

    result = asyncio.run(
        async_backfill_cyclic_component_history(
            hass,
            entry,
            store,
            history,
            as_of=dt.datetime.now(dt.UTC),
            marker_store=marker,
        )
    )

    assert result == history


def test_recorder_executor_finishes_before_lifecycle_cancel_propagates():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class _Hass:
        @staticmethod
        async def async_add_executor_job(target, *args):
            return await asyncio.to_thread(target, *args)

    def _query():
        started.set()
        release.wait(timeout=2)
        finished.set()
        return "complete"

    async def _run():
        task = asyncio.create_task(_await_executor_completion(_Hass(), _query))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(_run())


async def _async_value(value):
    return value


def test_backfill_does_not_carry_power_across_an_unavailable_transition():
    history = (_slot(0),)

    changed = backfill_component_power_history(
        history,
        {"dryer": ((0.0, 1_000.0), (300.0, None), (600.0, 0.0))},
    )

    assert changed == ()


def test_backfill_does_not_hold_positive_power_across_a_silent_gap():
    history = (_slot(0),)

    changed = backfill_component_power_history(
        history,
        {"dryer": ((0.0, 1_000.0), (600.0, 0.0))},
    )

    assert changed == ()


def test_backfill_may_hold_a_confirmed_zero_state_across_a_quiet_slot():
    history = (_slot(0),)

    changed = backfill_component_power_history(history, {"dryer": ((0.0, 0.0),)})

    assert changed[0].load_components[0].energy_kwh == 0.0


def test_backfill_mismatch_does_not_change_existing_component_quality():
    existing = LoadComponentEnergy("heat_pump", 0.4, DataQuality())
    history = (_slot(0, (existing,)),)

    changed = backfill_component_power_history(
        history,
        {"dryer": tuple((float(offset), 1_000.0) for offset in range(0, 900, 120))},
    )

    components = {item.component_key: item for item in changed[0].load_components}
    assert components["heat_pump"].quality.flags == ()
    assert QualityFlag.COMPONENT_MISMATCH in components["dryer"].quality.flags


def test_background_history_update_applies_after_control_is_running():
    old = (_slot(0),)
    new = (_slot(SLOT_MS),)

    class _Hass:
        @staticmethod
        def async_create_task(coroutine, **_kwargs):
            return asyncio.create_task(coroutine)

    class _Planning:
        def __init__(self):
            self.environment = None

        def set_forecast_environment(self, *values):
            self.environment = values

    async def _run():
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator.hass = _Hass()
        coordinator._feature_history_task = None
        coordinator._feature_history = old
        coordinator._planning_pipeline = _Planning()
        coordinator._weather = ()
        coordinator._load_components = SimpleNamespace(drivers=(), specs=())
        coordinator._unloading = False

        async def _updated():
            return new

        coordinator.async_start_feature_history_update(_updated())
        await coordinator._feature_history_task
        return coordinator

    coordinator = asyncio.run(_run())

    assert coordinator._feature_history == (*old, *new)
    assert coordinator._planning_pipeline.environment[0] == (*old, *new)


def test_background_merge_preserves_newer_slots_and_richer_backfill():
    existing = LoadComponentEnergy("heat_pump", 0.1, DataQuality())
    backfilled = LoadComponentEnergy("dryer", 0.2, DataQuality())
    current = (_slot(0, (existing,)), _slot(SLOT_MS))
    update = (_slot(0, (existing, backfilled)),)

    merged = merge_feature_history(current, update)

    assert {item.component_key for item in merged[0].load_components} == {
        "heat_pump",
        "dryer",
    }
    assert merged[1] == current[1]


def test_background_merge_only_adds_components_to_current_slot_facts():
    current = _slot(0)
    repaired = HistoricalFeatureSlot(
        current.slot,
        house_load_no_ev_kwh=0.75,
        pv_generation_kwh=0.2,
        grid_import_kwh=0.55,
        grid_export_kwh=0.0,
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        ev_charge_kwh=0.0,
        price_ct_per_kwh=25.0,
        quality=DataQuality(),
    )
    stale_backfill = replace(
        current,
        load_components=(LoadComponentEnergy("dryer", 0.2, DataQuality()),),
    )

    merged = merge_feature_history((repaired,), (stale_backfill,))

    assert merged[0].house_load_no_ev_kwh == 0.75
    assert merged[0].pv_generation_kwh == 0.2
    assert merged[0].price_ct_per_kwh == 25.0
    assert merged[0].load_components == stale_backfill.load_components

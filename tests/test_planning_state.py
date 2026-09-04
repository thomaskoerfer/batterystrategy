"""Typed planning-state ownership and schema-11 persistence tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.battery_strategy.optimizer_state import (
    load_state_document,
    save_state_document,
)
from custom_components.battery_strategy.planning_runtime import PlanningRuntime
from custom_components.battery_strategy.planning_state import (
    STATE_SCHEMA_VERSION,
    PlanningOwnerState,
    PlanningStateStore,
    StalePlanningStateLease,
)


def _runtime(tmp_path: Path, captured_at_ms: int, store: PlanningStateStore):
    return PlanningRuntime.from_mapping(
        {
            "captured_at_ms": captured_at_ms,
            "config_dir": str(tmp_path),
            "state_store": store,
        }
    )


def test_store_round_trip_preserves_schema_11_keys_and_unknown_salvage(tmp_path):
    path = tmp_path / "battery_strategy_optimizer_state.json"
    source = {
        "samples": [],
        "predictions": [],
        "backtests": [],
        "pv_bias": 1.1,
        "load_bias": 0.9,
        "pv_bias_slots": [1.0] * 96,
        "load_bias_slots": [1.0] * 96,
        "virtual_energy_kwh": 3.0,
        "virtual_last_ts": None,
        "virtual_last_mode": "idle",
        "virtual_last_power_w": 0.0,
        "virtual_trace": [],
        "last_known_soc_pct": 50.0,
        "eex_cache": {},
        "daily_savings": {},
        "actual_daily_savings": {},
        "last_output": {},
        "state_schema": STATE_SCHEMA_VERSION,
        "unknown_retained_key": {"value": 7},
    }
    save_state_document(path, source)
    store = PlanningStateStore(str(path))
    state = store.load(_runtime(tmp_path, 1_800_000_000_000, store))

    assert isinstance(state, PlanningOwnerState)
    assert store.save(state)
    assert load_state_document(path) == source


def test_new_lifecycle_generation_rejects_stale_writer(tmp_path):
    path = tmp_path / "battery_strategy_optimizer_state.json"
    first = PlanningStateStore.claim(path)
    runtime = _runtime(tmp_path, 1_800_000_000_000, first)
    state = first.load(runtime)
    PlanningStateStore.claim(path)

    with pytest.raises(StalePlanningStateLease):
        first.save(state)


def test_revoked_lifecycle_rejects_late_executor_write(tmp_path):
    path = tmp_path / "battery_strategy_optimizer_state.json"
    store = PlanningStateStore.claim(path)
    state = store.load(_runtime(tmp_path, 1_800_000_000_000, store))

    store.revoke()

    with pytest.raises(StalePlanningStateLease):
        store.save(state)


def test_older_run_cannot_replace_newer_persisted_output(tmp_path):
    path = tmp_path / "battery_strategy_optimizer_state.json"
    store = PlanningStateStore(str(path))
    runtime = _runtime(tmp_path, 1_800_000_000_000, store)
    state = store.load(runtime)
    save_state_document(
        path,
        {
            "last_output": {"timestamp": "2027-01-15T10:15:01+00:00"},
            "state_schema": STATE_SCHEMA_VERSION,
        },
    )

    assert store.save(state) is False
    assert load_state_document(path)["last_output"]["timestamp"] == (
        "2027-01-15T10:15:01+00:00"
    )


def test_malformed_typed_field_recovers_to_safe_empty_state(tmp_path):
    path = tmp_path / "battery_strategy_optimizer_state.json"
    save_state_document(
        path,
        {
            "pv_bias": "broken",
            "last_output": {"mode": "discharge_planned"},
            "state_schema": STATE_SCHEMA_VERSION,
        },
    )
    store = PlanningStateStore(str(path))

    state = store.load(_runtime(tmp_path, 1_800_000_000_000, store))

    assert state.forecast.pv_bias == 1.0
    assert state.publication.last_output == {}

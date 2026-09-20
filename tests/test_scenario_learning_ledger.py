"""Tests for the bounded, observational scenario-learning ledger."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from dataclasses import replace

from custom_components.battery_strategy import scenario_learning_ledger
from custom_components.battery_strategy.contracts import (
    EvForecastSlot,
    QuantileEnergy,
    ScenarioBuildRequest,
)
from custom_components.battery_strategy.economic_optimizer import (
    UnifiedScenarioOptimizer,
)
from custom_components.battery_strategy.scenario_generation import ScenarioBuilder
from custom_components.battery_strategy.scenario_learning_ledger import (
    SCHEMA_VERSION,
    append_scenario_learning_vintage,
)
from tests.test_economic_optimizer import problem
from tests.test_scenario_builder import actual, bundle, evidence


def _case():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start)
    history = []
    for weeks in range(1, 5):
        for index in range(2):
            history.append(
                actual(
                    start
                    - dt.timedelta(weeks=weeks)
                    + dt.timedelta(minutes=15 * index),
                    load=0.3 + weeks * 0.01,
                    pv=0.1,
                    ev=0.0,
                )
            )
    request = ScenarioBuildRequest(forecast, evidence(start, history, forecast))
    build = ScenarioBuilder().build(request)
    candidate = problem(
        [20.0] * len(request.forecast.load.slots),
        loads=[item.energy.p50_kwh for item in request.forecast.load.slots],
        pv=[item.energy.p50_kwh for item in request.forecast.pv.slots],
        start_ms=request.forecast.load.slots[0].slot.start_ms,
    )
    candidate = replace(candidate, scenarios=build.scenarios)
    result = UnifiedScenarioOptimizer().optimize(candidate)
    return build, candidate, result


def test_learning_ledger_is_hourly_bounded_and_observational(tmp_path):
    build, candidate, result = _case()

    first = append_scenario_learning_vintage(
        tmp_path,
        build_result=build,
        optimization_problem=candidate,
        optimization_result=result,
    )
    second = append_scenario_learning_vintage(
        tmp_path,
        build_result=build,
        optimization_problem=candidate,
        optimization_result=result,
    )

    assert first is not None
    assert second is None
    with gzip.open(first, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["observational_only"] is True
    assert payload["scenario_build"]["candidates"]
    assert payload["decision"]["slot_start_ms"] == candidate.market[0].slot.start_ms


def test_learning_ledger_records_failure_and_ev_state_boundary(tmp_path):
    build, candidate, _result = _case()
    first = append_scenario_learning_vintage(
        tmp_path,
        build_result=build,
        optimization_problem=candidate,
        optimization_result=None,
    )
    assert first is not None
    active_ev = replace(
        candidate.forecast.ev,
        slots=tuple(
            EvForecastSlot(slot.slot, QuantileEnergy(0.2), 1.0, 0.0)
            for slot in candidate.forecast.ev.slots
        ),
    )
    active_problem = replace(
        candidate,
        forecast=replace(candidate.forecast, ev=active_ev),
        scenarios=None,
    )
    event = append_scenario_learning_vintage(
        tmp_path,
        build_result=build,
        optimization_problem=active_problem,
        optimization_result=None,
    )

    assert event is not None
    assert "-ev-" in event.name
    with gzip.open(first, "rt", encoding="utf-8") as handle:
        assert json.load(handle)["optimization_status"] == "failed"


def test_learning_ledger_prunes_by_age_and_size(tmp_path, monkeypatch):
    build, candidate, result = _case()
    old = tmp_path / "2000/01/01/0000.json.gz"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    os.utime(old, (0, 0))
    monkeypatch.setattr(scenario_learning_ledger, "RETENTION_DAYS", 1)
    append_scenario_learning_vintage(
        tmp_path,
        build_result=build,
        optimization_problem=candidate,
        optimization_result=result,
    )
    assert not old.exists()

    another = tmp_path / "2000/01/02/0000.json.gz"
    another.parent.mkdir(parents=True)
    another.write_bytes(b"large")
    monkeypatch.setattr(scenario_learning_ledger, "RETENTION_DAYS", 400)
    monkeypatch.setattr(scenario_learning_ledger, "MAX_BYTES", 1)
    scenario_learning_ledger._prune(tmp_path, candidate.as_of_ms)
    assert sum(path.stat().st_size for path in tmp_path.rglob("*.json.gz")) <= 1

"""Tests for the bounded, observational scenario-learning ledger."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from dataclasses import replace

from custom_components.battery_strategy.contracts import ScenarioBuildRequest
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


def test_learning_ledger_is_hourly_bounded_and_observational(tmp_path):
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

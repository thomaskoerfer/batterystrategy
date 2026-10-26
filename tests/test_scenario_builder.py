"""Behavioral tests for the public Scenario Builder seam."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from custom_components.battery_strategy.contracts import (
    DataQuality,
    EvForecast,
    EvForecastSlot,
    ForecastDistributionBundle,
    ForecastSlot,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadForecast,
    LoadForecastComponent,
    PvForecast,
    QualityFlag,
    QuantileEnergy,
    ResidualCohort,
    ScenarioBuildRequest,
    ScenarioBuildStatus,
    ScenarioEvidenceSnapshot,
    ScenarioGenerationSettings,
    SlotKey,
)
from custom_components.battery_strategy.forecasting.uncertainty import (
    PV_SERIES,
    TOTAL_LOAD_SERIES,
    cohort_key,
)
from custom_components.battery_strategy.scenario_generation import ScenarioBuilder
from custom_components.battery_strategy.scenario_generation.engine import (
    _repair_continuous,
    _repair_ev,
)

SLOT_MS = 15 * 60 * 1000


def key(value: dt.datetime) -> SlotKey:
    start = int(value.timestamp() * 1000)
    return SlotKey(start, start + SLOT_MS)


def actual(
    value: dt.datetime,
    *,
    load: float,
    pv: float,
    ev: float,
    quality: DataQuality = DataQuality(),
    component_quality: DataQuality = DataQuality(),
    component_energy: float = 0.1,
) -> HistoricalFeatureSlot:
    return HistoricalFeatureSlot(
        key(value),
        load,
        pv,
        0.0,
        0.0,
        0.0,
        0.0,
        ev,
        None,
        quality,
        (LoadComponentEnergy("dishwasher", component_energy, component_quality),),
    )


def bundle(
    start: dt.datetime,
    count: int = 2,
    *,
    ev_active: bool = False,
    ev_probability: float | None = None,
):
    slots = tuple(
        key(start + dt.timedelta(minutes=15 * index)) for index in range(count)
    )
    generated = int(start.timestamp() * 1000)
    load = LoadForecast(
        "load",
        generated,
        generated,
        "load-v1",
        tuple(ForecastSlot(slot, QuantileEnergy(0.4, 0.2, 0.6, 12)) for slot in slots),
    )
    pv = PvForecast(
        "pv",
        generated,
        generated,
        "pv-v1",
        tuple(ForecastSlot(slot, QuantileEnergy(0.2, 0.0, 0.4, 12)) for slot in slots),
    )
    ev_energy = (
        QuantileEnergy(0.25, 0.0, 0.5, 12)
        if ev_active
        else QuantileEnergy(0.0, 0.0, 0.4, 12)
    )
    ev = EvForecast(
        "ev",
        generated,
        generated,
        "ev-v1",
        tuple(
            EvForecastSlot(
                slot,
                ev_energy,
                ev_probability
                if ev_probability is not None
                else (1.0 if ev_active else 0.2),
                0.2,
            )
            for slot in slots
        ),
    )
    return ForecastDistributionBundle(load, pv, ev)


def evidence(start: dt.datetime, history, forecast, *, cohorts: bool = True):
    values = (-0.2,) * 6 + (0.2,) * 6
    residuals = []
    if cohorts:
        residuals = [
            ResidualCohort(cohort_key(TOTAL_LOAD_SERIES, "load-v1", 0), values),
            ResidualCohort(cohort_key(PV_SERIES, "pv-v1", 0), values),
        ]
    generated = int(start.timestamp() * 1000)
    return ScenarioEvidenceSnapshot(
        "evidence",
        generated,
        generated,
        "UTC",
        tuple(history),
        tuple(residuals),
        1.0,
        0.075,
    )


def test_builder_preserves_joint_weekly_paths_and_keeps_ev_separate():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start)
    history = []
    for weeks, loads, pvs, evs in (
        (1, (0.2, 0.3), (0.1, 0.0), (0.0, 0.0)),
        (2, (0.6, 0.7), (0.3, 0.4), (0.0, 0.4)),
    ):
        for index in range(2):
            history.append(
                actual(
                    start
                    - dt.timedelta(weeks=weeks)
                    + dt.timedelta(minutes=15 * index),
                    load=loads[index],
                    pv=pvs[index],
                    ev=evs[index],
                )
            )
    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(2, 12),
        )
    )
    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert len(result.scenarios.scenarios) == 2
    assert sum(item.probability for item in result.scenarios.scenarios) == 1.0
    assert all(
        path.slots[0].ev_charge_kwh == 0.0 for path in result.scenarios.scenarios
    )


def test_missing_component_value_does_not_reject_valid_aggregate_path():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1)
    bad_component = DataQuality(0.0, (QualityFlag.ESTIMATED,))
    history = tuple(
        actual(
            start - dt.timedelta(weeks=weeks),
            load=0.4,
            pv=0.2,
            ev=0.0,
            component_quality=bad_component,
        )
        for weeks in (1, 2)
    )
    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(2, 12),
        )
    )
    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.diagnostics.excluded_component_values == 2


def test_missing_ev_evidence_does_not_reject_load_and_pv_paths():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1)
    bad_ev = DataQuality(1.0, (QualityFlag.MISSING_EV,))
    history = tuple(
        actual(
            start - dt.timedelta(weeks=weeks),
            load=0.4,
            pv=0.2,
            ev=0.0,
            quality=bad_ev,
        )
        for weeks in (1, 2)
    )
    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(2, 12),
        )
    )
    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert len(result.scenarios.scenarios) == 2
    assert all(
        path.slots[0].house_load_kwh >= 0.0 and path.slots[0].pv_generation_kwh >= 0.0
        for path in result.scenarios.scenarios
    )
    assert result.diagnostics.eligible is True
    assert ("ev:forecast_only", 2) in result.diagnostics.marginal_sources


def test_insufficient_evidence_retains_valid_candidate_diagnostics():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1)
    history = (
        actual(
            start - dt.timedelta(weeks=1),
            load=0.4,
            pv=0.2,
            ev=0.0,
        ),
    )
    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(2, 12),
        )
    )

    assert result.status is ScenarioBuildStatus.INSUFFICIENT_EVIDENCE
    assert len(result.diagnostics.candidates) == 1
    assert result.diagnostics.candidates[0].selected is True


def test_active_ev_boundary_falls_back_without_rejecting_other_channels():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1, ev_active=True)
    history = (
        actual(start - dt.timedelta(weeks=1), load=0.4, pv=0.2, ev=0.001),
        actual(start - dt.timedelta(weeks=2), load=0.4, pv=0.2, ev=0.2),
    )

    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(1, 12),
        )
    )

    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert result.diagnostics.candidate_paths == 2
    assert ("ev:historical_donor", 1) in result.diagnostics.marginal_sources
    assert ("ev:historical_same_path", 1) in result.diagnostics.marginal_sources
    assert all(
        path.slots[0].ev_charge_kwh >= 0.075 for path in result.scenarios.scenarios
    )


def test_ev_donor_selection_uses_candidates_outside_load_pv_path_limit():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1, ev_active=True)
    history = tuple(
        actual(
            start - dt.timedelta(weeks=weeks),
            load=0.4,
            pv=0.2,
            ev=0.2 if weeks == 13 else 0.0,
        )
        for weeks in range(1, 14)
    )

    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(12, 12),
        )
    )

    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert ("ev:historical_donor", 12) in result.diagnostics.marginal_sources
    assert all(
        path.slots[0].ev_charge_kwh >= 0.075 for path in result.scenarios.scenarios
    )


def test_ev_donor_selection_ignores_unrelated_load_quality():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1, ev_active=True)
    bad_load = DataQuality(1.0, (QualityFlag.MISSING_GRID,))
    history = tuple(
        actual(
            start - dt.timedelta(weeks=weeks),
            load=0.4,
            pv=0.2,
            ev=0.2 if weeks == 13 else 0.0,
            quality=bad_load if weeks == 13 else DataQuality(),
        )
        for weeks in range(1, 14)
    )

    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(12, 12),
        )
    )

    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert ("ev:historical_donor", 12) in result.diagnostics.marginal_sources
    assert all(
        path.slots[0].ev_charge_kwh >= 0.075 for path in result.scenarios.scenarios
    )


def test_ev_donor_repairs_remain_visible_without_affecting_path_weights():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 51, ev_active=True)
    history = []
    for weeks in (1, 2, 3):
        for index in range(51):
            quality = DataQuality()
            ev = 0.0 if weeks in (1, 2) else 0.2
            if weeks == 3 and index == 24:
                quality = DataQuality(1.0, (QualityFlag.MISSING_GRID,))
            elif weeks == 3 and index == 25:
                quality = DataQuality(
                    1.0,
                    (QualityFlag.MISSING_GRID, QualityFlag.MISSING_EV),
                )
            history.append(
                actual(
                    start
                    - dt.timedelta(weeks=weeks)
                    + dt.timedelta(minutes=15 * index),
                    load=0.4,
                    pv=0.2,
                    ev=ev,
                    quality=quality,
                )
            )

    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(2, 2),
        )
    )

    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert result.diagnostics.repaired_slots == 2
    assert ("ev:historical_donor", 2) in result.diagnostics.marginal_sources
    assert ("ev:repaired_slot", 2) in result.diagnostics.marginal_sources
    assert all(item.raw_weight == 1.0 for item in result.diagnostics.candidates)
    assert all(item.repaired_slots == 1 for item in result.diagnostics.candidates)


def test_ev_scenario_occurrence_tracks_forecast_probability():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1, ev_probability=0.5)
    history = tuple(
        actual(
            start - dt.timedelta(weeks=weeks),
            load=0.4,
            pv=0.2,
            ev=0.1 * weeks,
        )
        for weeks in (1, 2, 3, 4)
    )

    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(4, 12),
        )
    )

    assert result.scenarios is not None
    active_probability = sum(
        path.probability
        for path in result.scenarios.scenarios
        if path.slots[0].ev_charge_kwh >= 0.075
    )
    assert active_probability == 0.5
    assert result.diagnostics.ev_probability_max_error == 0.0


def test_component_similarity_changes_analogue_weight_without_rejecting_paths():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1)
    component_slots = (ForecastSlot(forecast.load.slots[0].slot, QuantileEnergy(0.4)),)
    forecast = replace(
        forecast,
        load=replace(
            forecast.load,
            components=(
                LoadForecastComponent(
                    "dishwasher",
                    "dishwasher-v1",
                    forecast.load.training_cutoff_ms,
                    component_slots,
                ),
            ),
        ),
    )
    history = (
        actual(
            start - dt.timedelta(weeks=1),
            load=0.4,
            pv=0.2,
            ev=0.0,
            component_energy=0.4,
        ),
        actual(
            start - dt.timedelta(weeks=2),
            load=0.4,
            pv=0.2,
            ev=0.0,
            component_energy=0.1,
        ),
    )

    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast),
            ScenarioGenerationSettings(2, 12),
        )
    )

    assert result.scenarios is not None
    probabilities = {
        path.scenario_id: path.probability for path in result.scenarios.scenarios
    }
    assert probabilities["week-1-0"] > probabilities["week-2-1"]


def test_builder_uses_emitted_quantiles_when_residual_cohort_is_not_mature():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1)
    history = tuple(
        actual(start - dt.timedelta(weeks=weeks), load=0.4, pv=0.2, ev=0.0)
        for weeks in (1, 2)
    )
    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            evidence(start, history, forecast, cohorts=False),
            ScenarioGenerationSettings(2, 12),
        )
    )
    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert result.diagnostics.fallback_marginal_slots == 2
    assert result.diagnostics.marginal_sources == (
        ("ev:historical_same_path", 2),
        ("load:forecast_quantiles", 1),
        ("pv:forecast_quantiles", 1),
    )


def test_input_fingerprint_changes_with_candidate_evidence():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1)
    history = tuple(
        actual(start - dt.timedelta(weeks=weeks), load=0.4, pv=0.2, ev=0.0)
        for weeks in (1, 2)
    )
    request = ScenarioBuildRequest(
        forecast,
        evidence(start, history, forecast),
        ScenarioGenerationSettings(2, 12),
    )
    changed_history = (replace(history[0], house_load_no_ev_kwh=0.5), history[1])
    changed = replace(
        request,
        evidence=replace(request.evidence, history=changed_history),
    )

    first = ScenarioBuilder().build(request)
    second = ScenarioBuilder().build(changed)

    assert first.diagnostics.input_fingerprint != second.diagnostics.input_fingerprint


def test_evidence_rejects_history_after_training_cutoff():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    history = actual(start - dt.timedelta(weeks=1), load=0.4, pv=0.2, ev=0.0)

    with pytest.raises(ValueError, match="history cannot exceed"):
        ScenarioEvidenceSnapshot(
            "evidence",
            int(start.timestamp() * 1000),
            history.slot.start_ms,
            "UTC",
            (history,),
            (),
            1.0,
            0.075,
        )


def test_builder_rejects_evidence_newer_than_earliest_marginal_vintage():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    forecast = bundle(start, 1)
    forecast = replace(
        forecast,
        pv=replace(forecast.pv, generated_at_ms=forecast.pv.generated_at_ms + 60_000),
        ev=replace(forecast.ev, generated_at_ms=forecast.ev.generated_at_ms + 60_000),
    )
    history = tuple(
        actual(start - dt.timedelta(weeks=weeks), load=0.4, pv=0.2, ev=0.0)
        for weeks in (1, 2)
    )
    original = evidence(start, history, forecast)
    newer = replace(original, captured_at_ms=original.captured_at_ms + 30_000)

    result = ScenarioBuilder().build(
        ScenarioBuildRequest(
            forecast,
            newer,
            ScenarioGenerationSettings(2, 12),
        )
    )

    assert result.status is ScenarioBuildStatus.INVALID_INPUT
    assert result.diagnostics.eligible is False


def test_bounded_restart_run_is_repaired_as_one_channel_gap():
    values, repaired = _repair_continuous(
        (1.0, 0.0, 0.0, 4.0), (True, False, False, True), 2
    )
    assert values == pytest.approx((1.0, 2.0, 3.0, 4.0))
    assert repaired == frozenset({1, 2})


def test_ev_gap_classifies_state_but_does_not_interpolate_energy():
    values, repaired = _repair_ev(
        (0.2, 0.0, 0.0, 0.6), (True, False, False, True), 2, 0.075
    )
    assert values == (0.2, 0.075, 0.075, 0.6)
    assert repaired == frozenset({1, 2})

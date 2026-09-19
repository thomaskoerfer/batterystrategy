#!/usr/bin/env python3
"""Evaluate stochastic optimizer shadows against finalized actuals."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    BatteryState,
    CommercialPolicy,
    EvInteractionPolicy,
    ForecastBundle,
    ForecastScenario,
    ForecastScenarioSet,
    ForecastScenarioSlot,
    ForecastSlot,
    LoadForecast,
    MarketSlot,
    OptimizationProblem,
    PvForecast,
    QuantileEnergy,
    SlotKey,
)
from custom_components.battery_strategy.economic_optimizer import (
    StochasticDynamicProgrammingOptimizer,
)
from custom_components.battery_strategy.forecasting.uncertainty import lead_bucket

SLOT_MS = 15 * 60 * 1000
# HA updates every ten seconds. The first completed planning capture after a
# quarter-hour boundary is the operational boundary decision, even though its
# timestamp naturally includes scheduler latency.
BOUNDARY_DECISION_TOLERANCE_MS = 30_000
TRACE_SCHEMA = 3
DAY_MS = 24 * 60 * 60 * 1000
DEFAULT_TRACE_DIRECTORY = "/config/battery_strategy_forecast_trace"
DEFAULT_FEATURE_STORE = "/config/battery_strategy_features.json.gz"
INVALID_FLAGS = frozenset(
    {
        "estimated",
        "missing_grid",
        "missing_pv",
        "missing_battery",
        "missing_ev",
        "counter_reset",
        "restart_gap",
    }
)


@dataclass(frozen=True, slots=True)
class ScenarioScore:
    """One matured scenario marginal compared with its actual."""

    series: str
    crps_kwh: float
    covered_80: bool
    lead_bucket: int = 0
    regime: str = "all"
    p50_abs_error_kwh: float = 0.0
    event_probability: float | None = None
    event_actual: float | None = None
    target_start_ms: int = 0


@dataclass(frozen=True, slots=True)
class DecisionScore:
    """One vintage's executable first action against perfect foresight."""

    shadow_error_kwh: float
    authoritative_error_kwh: float
    shadow_direction_match: bool
    authoritative_direction_match: bool
    shadow_regret_eur: float
    authoritative_regret_eur: float
    block_day: int = 0


@dataclass(frozen=True, slots=True)
class PathScore:
    """One complete matured multivariate path forecast."""

    energy_score_kwh: float
    variogram_score: float
    ev_start_error_slots: float
    ev_duration_error_slots: float
    p50_energy_score_kwh: float = 0.0
    p50_variogram_score: float = 0.0
    inactive_ev_start_error_slots: float = 0.0
    inactive_ev_duration_error_slots: float = 0.0


def load_traces(root: str | Path, start_ms: int, end_ms: int) -> list[dict]:
    """Load only complete current-schema stochastic shadow traces."""
    traces = []
    root = Path(root)
    if not root.exists():
        return traces
    for path in sorted(root.glob("????-??-??/*.json.gz")):
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()))
            generated = int(payload["generated_at_ms"])
            if (
                payload.get("schema_version") == TRACE_SCHEMA
                and payload.get("non_authoritative") is True
                and start_ms <= generated <= end_ms
            ):
                traces.append(payload)
        except OSError, EOFError, UnicodeError, ValueError, TypeError, KeyError:
            continue
    return traces


def load_actuals(path: str | Path) -> dict[int, dict]:
    """Load finalized actual slots from the compact feature store."""
    payload = json.loads(gzip.decompress(Path(path).read_bytes()))
    if int(payload.get("schema_version", -1)) not in {1, 2, 3}:
        raise ValueError("unsupported feature-store schema")
    return {
        int(item["start_ms"]): item
        for item in payload.get("slots", ())
        if isinstance(item, dict) and "start_ms" in item
    }


def score_scenarios(
    traces: list[dict], actuals: dict[int, dict], *, as_of_ms: int
) -> list[ScenarioScore]:
    """Score load/PV marginals and EV occurrence for all matured slots."""
    scores = []
    for trace in traces:
        scenario_payload = trace.get("scenarios") or {}
        paths = scenario_payload.get("paths") or ()
        ev_policy = (trace.get("optimization_problem") or {}).get("ev_policy") or {}
        ev_active_kwh = (
            float(ev_policy.get("ev_active_threshold_w", 300.0)) / 1000.0 * 0.25
        )
        load_rows = trace.get("load", {}).get("slots", ())
        pv_rows = trace.get("pv", {}).get("slots", ())
        for index, (slot, pv_row) in enumerate(zip(load_rows, pv_rows, strict=True)):
            start_ms, end_ms = int(slot[0]), int(slot[1])
            if (
                start_ms < int(trace["generated_at_ms"])
                and int(trace["generated_at_ms"]) - start_ms
                > BOUNDARY_DECISION_TOLERANCE_MS
            ):
                continue
            actual = _usable_actual(actuals.get(start_ms), end_ms, as_of_ms)
            if actual is None or not paths:
                continue
            probabilities = [float(path["probability"]) for path in paths]
            bucket = lead_bucket(int(trace["generated_at_ms"]), start_ms)
            for series, position, actual_key, p50 in (
                ("load", 1, "house_load_no_ev_kwh", float(slot[2])),
                ("pv", 2, "pv_generation_kwh", float(pv_row[2])),
            ):
                values = [float(path["slots"][index][position]) for path in paths]
                target = float(actual[actual_key])
                scores.append(
                    ScenarioScore(
                        series,
                        _weighted_crps(values, probabilities, target),
                        _weighted_quantile(values, probabilities, 0.1)
                        <= target
                        <= _weighted_quantile(values, probabilities, 0.9),
                        bucket,
                        "active" if p50 > 1e-9 else "inactive",
                        abs(p50 - target),
                    )
                )
            ev_probability = sum(
                probability
                for probability, path in zip(probabilities, paths, strict=True)
                if float(path["slots"][index][3]) >= ev_active_kwh
            )
            ev_actual = float(float(actual.get("ev_charge_kwh", 0.0)) >= ev_active_kwh)
            scores.append(
                ScenarioScore(
                    "ev_event",
                    0.0,
                    True,
                    bucket,
                    "active" if ev_probability >= 0.5 else "inactive",
                    event_probability=ev_probability,
                    event_actual=ev_actual,
                    target_start_ms=start_ms,
                )
            )
    return scores


def score_decisions(
    traces: list[dict],
    actuals: dict[int, dict],
    *,
    as_of_ms: int,
    stride: int = 4,
) -> list[DecisionScore]:
    """Compare sampled first actions with full-horizon perfect foresight."""
    scores = []
    for trace in traces[:: max(1, stride)]:
        shadow = (trace.get("optimizer_plans") or {}).get("shadow")
        authoritative = (trace.get("optimizer_plans") or {}).get("authoritative")
        problem_payload = trace.get("optimization_problem")
        if not shadow or not authoritative or not problem_payload:
            continue
        first_start_ms = int(trace["load"]["slots"][0][0])
        elapsed_ms = int(trace["generated_at_ms"]) - first_start_ms
        if not 0 <= elapsed_ms <= BOUNDARY_DECISION_TOLERANCE_MS:
            continue
        problem = _perfect_foresight_problem(trace, actuals, as_of_ms=as_of_ms)
        if problem is None:
            continue
        optimizer = StochasticDynamicProgrammingOptimizer()
        perfect, perfect_diagnostics = optimizer.optimize_with_diagnostics(problem)
        perfect_action = _policy_vector(perfect.slots[0])
        shadow_action = _serialized_policy(shadow["slots"][0])
        authoritative_action = _serialized_policy(authoritative["slots"][0])
        optimum = perfect_diagnostics["expected_scenario_cost_eur"]
        shadow_cost = _constrained_objective(optimizer, problem, shadow["slots"][0])
        authoritative_cost = _constrained_objective(
            optimizer, problem, authoritative["slots"][0]
        )
        scores.append(
            DecisionScore(
                _policy_distance(shadow_action, perfect_action),
                _policy_distance(authoritative_action, perfect_action),
                _policy_direction(shadow_action) == _policy_direction(perfect_action),
                _policy_direction(authoritative_action)
                == _policy_direction(perfect_action),
                max(0.0, shadow_cost - optimum),
                max(0.0, authoritative_cost - optimum),
                first_start_ms // DAY_MS,
            )
        )
    return scores


def score_paths(
    traces: list[dict], actuals: dict[int, dict], *, as_of_ms: int
) -> list[PathScore]:
    """Score temporal and cross-series coherence on complete matured suffixes."""
    scores = []
    for trace in traces:
        paths = (trace.get("scenarios") or {}).get("paths") or ()
        rows = trace.get("load", {}).get("slots", ())
        if not paths or not rows:
            continue
        first = next(
            (
                index
                for index, row in enumerate(rows)
                if int(row[0]) + BOUNDARY_DECISION_TOLERANCE_MS
                >= int(trace["generated_at_ms"])
            ),
            None,
        )
        if first is None:
            continue
        matured = []
        for row in rows[first:]:
            actual = _usable_actual(actuals.get(int(row[0])), int(row[1]), as_of_ms)
            if actual is None:
                matured = []
                break
            matured.append(actual)
        if not matured:
            continue
        probabilities = [float(path["probability"]) for path in paths]
        actual_vector = _path_vector(matured)
        scenario_vectors = [
            _scenario_path_vector(path["slots"][first:]) for path in paths
        ]
        pv_rows = trace.get("pv", {}).get("slots", ())
        p50_vector = tuple(
            value
            for load_row, pv_row in zip(rows[first:], pv_rows[first:], strict=True)
            for value in (float(load_row[2]), float(pv_row[2]), 0.0)
        )
        ev_policy = (trace.get("optimization_problem") or {}).get("ev_policy") or {}
        threshold = float(ev_policy.get("ev_active_threshold_w", 300.0)) / 4000.0
        actual_start, actual_duration = _ev_shape(
            [float(item.get("ev_charge_kwh", 0.0)) for item in matured], threshold
        )
        expected_start_error = 0.0
        expected_duration_error = 0.0
        for probability, path in zip(probabilities, paths, strict=True):
            start, duration = _ev_shape(
                [float(row[3]) for row in path["slots"][first:]], threshold
            )
            expected_start_error += probability * abs(start - actual_start)
            expected_duration_error += probability * abs(duration - actual_duration)
        scores.append(
            PathScore(
                _energy_score(scenario_vectors, probabilities, actual_vector),
                _variogram_score(scenario_vectors, probabilities, actual_vector),
                expected_start_error,
                expected_duration_error,
                _energy_score([p50_vector], [1.0], actual_vector),
                _variogram_score([p50_vector], [1.0], actual_vector),
                abs(float(len(matured)) - actual_start),
                abs(actual_duration),
            )
        )
    return scores


def summarize(
    traces: list[dict],
    scenario_scores: list[ScenarioScore],
    decisions: list[DecisionScore],
    path_scores: list[PathScore] | None = None,
) -> dict[str, object]:
    """Return the release-gate metrics without asserting policy thresholds."""
    scenario_metrics = {}
    for series in ("load", "pv"):
        selected = [item for item in scenario_scores if item.series == series]
        scenario_metrics[series] = {
            "samples": len(selected),
            "mean_crps_kwh": (
                sum(item.crps_kwh for item in selected) / len(selected)
                if selected
                else None
            ),
            "p10_p90_coverage_pct": (
                100.0 * sum(item.covered_80 for item in selected) / len(selected)
                if selected
                else None
            ),
            "p50_mean_abs_error_kwh": (
                sum(item.p50_abs_error_kwh for item in selected) / len(selected)
                if selected
                else None
            ),
        }
    cohorts = {}
    for item in scenario_scores:
        if item.series not in {"load", "pv"}:
            continue
        key = f"{item.series}:lead_{item.lead_bucket}:{item.regime}"
        selected = [
            candidate
            for candidate in scenario_scores
            if candidate.series == item.series
            and candidate.lead_bucket == item.lead_bucket
            and candidate.regime == item.regime
        ]
        cohorts[key] = {
            "samples": len(selected),
            "mean_crps_kwh": sum(value.crps_kwh for value in selected) / len(selected),
            "p50_mean_abs_error_kwh": sum(value.p50_abs_error_kwh for value in selected)
            / len(selected),
            "p10_p90_coverage_pct": 100.0
            * sum(value.covered_80 for value in selected)
            / len(selected),
        }
    ev = [item for item in scenario_scores if item.series == "ev_event"]
    runtimes = [
        float(item["shadow_evaluation"]["runtime_ms"])
        for item in traces
        if (item.get("shadow_evaluation") or {}).get("runtime_ms") is not None
    ]
    paths = path_scores or []
    regret_mean, regret_low, regret_high = _daily_block_bootstrap_summary(decisions)
    ev_brier = (
        sum((item.event_probability - item.event_actual) ** 2 for item in ev) / len(ev)
        if ev
        else None
    )
    ev_prevalence = sum(item.event_actual for item in ev) / len(ev) if ev else None
    ev_climatology_brier = (
        ev_prevalence * (1.0 - ev_prevalence) if ev_prevalence is not None else None
    )
    ev_actual_by_slot = {
        item.target_start_ms: bool(item.event_actual)
        for item in ev
        if item.target_start_ms > 0 and item.event_actual is not None
    }
    ev_sessions = 0
    previous_active = False
    for active in (ev_actual_by_slot[key] for key in sorted(ev_actual_by_slot)):
        if active and not previous_active:
            ev_sessions += 1
        previous_active = active
    inactive_ev_slots = sum(not active for active in ev_actual_by_slot.values())
    report = {
        "non_authoritative": True,
        "trace_vintages": len(traces),
        "completed_shadow_vintages": sum(
            (item.get("shadow_evaluation") or {}).get("status") == "completed"
            for item in traces
        ),
        "scenario_metrics": scenario_metrics,
        "scenario_cohorts": cohorts,
        "ev_event_samples": len(ev),
        "ev_event_brier_score": ev_brier,
        "ev_climatology_brier_score": ev_climatology_brier,
        "matured_ev_sessions": ev_sessions,
        "inactive_ev_slots": inactive_ev_slots,
        "perfect_foresight_vintages": len(decisions),
        "shadow_first_action_mae_kwh": (
            sum(item.shadow_error_kwh for item in decisions) / len(decisions)
            if decisions
            else None
        ),
        "authoritative_first_action_mae_kwh": (
            sum(item.authoritative_error_kwh for item in decisions) / len(decisions)
            if decisions
            else None
        ),
        "shadow_direction_match_pct": (
            100.0
            * sum(item.shadow_direction_match for item in decisions)
            / len(decisions)
            if decisions
            else None
        ),
        "authoritative_direction_match_pct": (
            100.0
            * sum(item.authoritative_direction_match for item in decisions)
            / len(decisions)
            if decisions
            else None
        ),
        "shadow_mean_perfect_foresight_regret_eur": (
            sum(item.shadow_regret_eur for item in decisions) / len(decisions)
            if decisions
            else None
        ),
        "authoritative_mean_perfect_foresight_regret_eur": (
            sum(item.authoritative_regret_eur for item in decisions) / len(decisions)
            if decisions
            else None
        ),
        "paired_regret_delta_mean_eur": regret_mean,
        "paired_regret_delta_95pct_ci_eur": [regret_low, regret_high],
        "complete_path_vintages": len(paths),
        "mean_energy_score_kwh": (
            sum(item.energy_score_kwh for item in paths) / len(paths) if paths else None
        ),
        "mean_variogram_score": (
            sum(item.variogram_score for item in paths) / len(paths) if paths else None
        ),
        "p50_mean_energy_score_kwh": (
            sum(item.p50_energy_score_kwh for item in paths) / len(paths)
            if paths
            else None
        ),
        "p50_mean_variogram_score": (
            sum(item.p50_variogram_score for item in paths) / len(paths)
            if paths
            else None
        ),
        "ev_start_mae_slots": (
            sum(item.ev_start_error_slots for item in paths) / len(paths)
            if paths
            else None
        ),
        "ev_duration_mae_slots": (
            sum(item.ev_duration_error_slots for item in paths) / len(paths)
            if paths
            else None
        ),
        "inactive_ev_start_mae_slots": (
            sum(item.inactive_ev_start_error_slots for item in paths) / len(paths)
            if paths
            else None
        ),
        "inactive_ev_duration_mae_slots": (
            sum(item.inactive_ev_duration_error_slots for item in paths) / len(paths)
            if paths
            else None
        ),
        "shadow_runtime_mean_ms": sum(runtimes) / len(runtimes) if runtimes else None,
        "shadow_runtime_max_ms": max(runtimes) if runtimes else None,
    }
    mature_cohorts = [value for value in cohorts.values() if value["samples"] >= 30]
    vintages_per_day = Counter(
        int(item["generated_at_ms"]) // DAY_MS
        for item in traces
        if item.get("generated_at_ms") is not None
    )
    minimum_daily_vintages = math.ceil(0.9 * DAY_MS / SLOT_MS)
    complete_observation_days = sum(
        count >= minimum_daily_vintages for count in vintages_per_day.values()
    )
    finite_decisions = all(
        math.isfinite(item.shadow_regret_eur)
        and math.isfinite(item.authoritative_regret_eur)
        for item in decisions
    )
    all_cohorts_mature = bool(cohorts) and len(mature_cohorts) == len(cohorts)
    path_gate_ok = bool(paths) and (
        report["mean_energy_score_kwh"] <= report["p50_mean_energy_score_kwh"]
        and report["mean_variogram_score"] <= report["p50_mean_variogram_score"]
        and report["ev_start_mae_slots"] <= report["inactive_ev_start_mae_slots"]
        and report["ev_duration_mae_slots"] <= report["inactive_ev_duration_mae_slots"]
    )
    ev_gate_ok = (
        ev_sessions >= 3
        and inactive_ev_slots >= 30
        and ev_brier is not None
        and ev_climatology_brier is not None
        and ev_brier <= ev_climatology_brier + 1e-12
    )
    enough = (
        complete_observation_days >= 7
        and len(decisions) >= 100
        and len(paths) >= 20
        and all_cohorts_mature
        and regret_high is not None
    )
    failed = (
        not finite_decisions
        or (runtimes and max(runtimes) > 5000.0)
        or (regret_high is not None and regret_high > 0.0)
        or not path_gate_ok
        or not ev_gate_ok
        or any(
            not 65.0 <= value["p10_p90_coverage_pct"] <= 95.0
            or value["mean_crps_kwh"] > value["p50_mean_abs_error_kwh"]
            for value in mature_cohorts
        )
    )
    status = (
        "fail"
        if not finite_decisions
        else ("insufficient_data" if not enough else ("fail" if failed else "pass"))
    )
    report["release_gate"] = {
        "status": status,
        "minimum_decision_vintages": 100,
        "minimum_complete_path_vintages": 20,
        "minimum_cohort_samples": 30,
        "minimum_observation_days": 7,
        "complete_observation_days": complete_observation_days,
        "minimum_daily_vintages": minimum_daily_vintages,
        "minimum_matured_ev_sessions": 3,
        "minimum_inactive_ev_slots": 30,
        "maximum_runtime_ms": 5000.0,
    }
    return report


def _perfect_foresight_problem(
    trace: dict, actuals: dict[int, dict], *, as_of_ms: int
) -> OptimizationProblem | None:
    problem = trace["optimization_problem"]
    load_meta, pv_meta = trace["load"], trace["pv"]
    slots = []
    actual_path = []
    load_slots = []
    pv_slots = []
    for load_row, pv_row in zip(load_meta["slots"], pv_meta["slots"], strict=True):
        slot = SlotKey(int(load_row[0]), int(load_row[1]))
        actual = _usable_actual(actuals.get(slot.start_ms), slot.end_ms, as_of_ms)
        if actual is None:
            return None
        load = float(actual["house_load_no_ev_kwh"])
        pv = float(actual["pv_generation_kwh"])
        ev = float(actual.get("ev_charge_kwh", 0.0))
        slots.append(slot)
        load_slots.append(ForecastSlot(slot, QuantileEnergy(load)))
        pv_slots.append(ForecastSlot(slot, QuantileEnergy(pv)))
        actual_path.append(ForecastScenarioSlot(slot, load, pv, ev))
    generated = max(int(load_meta["generated_at_ms"]), int(pv_meta["generated_at_ms"]))
    scenario_set = ForecastScenarioSet(
        f"perfect:{generated}",
        generated,
        generated,
        "perfect-foresight-v1",
        (ForecastScenario("actual", 1.0, tuple(actual_path)),),
    )
    bundle = ForecastBundle(
        LoadForecast(
            "perfect-load",
            generated,
            generated,
            "perfect-v1",
            tuple(load_slots),
        ),
        PvForecast(
            "perfect-pv",
            generated,
            generated,
            "perfect-v1",
            tuple(pv_slots),
        ),
        scenario_set,
    )
    constraints = problem["constraints"]
    policy = problem["commercial_policy"]
    ev_policy = problem["ev_policy"]
    markets = tuple(
        MarketSlot(slot, float(row[1]), float(row[2]), str(row[3]))
        for slot, row in zip(slots, problem["market"], strict=True)
    )
    return OptimizationProblem(
        str(problem["problem_id"]),
        int(problem["as_of_ms"]),
        bundle,
        markets,
        BatteryState(
            int(problem["battery"]["captured_at_ms"]),
            float(problem["battery"]["soc_pct"]),
        ),
        BatteryConstraints(**constraints),
        CommercialPolicy(**policy),
        EvInteractionPolicy(**ev_policy),
    )


def _usable_actual(actual: dict | None, end_ms: int, as_of_ms: int) -> dict | None:
    if (
        actual is None
        or end_ms > as_of_ms
        or float(actual.get("coverage", 0.0)) < 0.999
    ):
        return None
    if {str(item) for item in actual.get("flags", ())} & INVALID_FLAGS:
        return None
    for key in ("house_load_no_ev_kwh", "pv_generation_kwh", "ev_charge_kwh"):
        try:
            value = float(actual.get(key, 0.0))
        except TypeError, ValueError:
            return None
        if not math.isfinite(value) or value < 0.0:
            return None
    return actual


def _weighted_quantile(values, weights, probability):
    ordered = sorted(zip(values, weights, strict=True))
    threshold = probability * sum(weights)
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def _weighted_crps(values, weights, actual):
    first = sum(
        weight * abs(value - actual)
        for value, weight in zip(values, weights, strict=True)
    )
    second = 0.5 * sum(
        left_weight * right_weight * abs(left - right)
        for left, left_weight in zip(values, weights, strict=True)
        for right, right_weight in zip(values, weights, strict=True)
    )
    return first - second


def _path_vector(actuals):
    return tuple(
        value
        for actual in actuals
        for value in (
            float(actual["house_load_no_ev_kwh"]),
            float(actual["pv_generation_kwh"]),
            float(actual.get("ev_charge_kwh", 0.0)),
        )
    )


def _scenario_path_vector(rows):
    return tuple(float(value) for row in rows for value in row[1:4])


def _euclidean(left, right):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def _energy_score(vectors, weights, actual):
    first = sum(
        weight * _euclidean(vector, actual)
        for vector, weight in zip(vectors, weights, strict=True)
    )
    second = 0.5 * sum(
        left_weight * right_weight * _euclidean(left, right)
        for left, left_weight in zip(vectors, weights, strict=True)
        for right, right_weight in zip(vectors, weights, strict=True)
    )
    return first - second


def _variogram_score(vectors, weights, actual):
    slot_count = len(actual) // 3
    pairs = []
    for series in range(3):
        for lag in (1, 4, 16):
            pairs.extend(
                (3 * slot + series, 3 * (slot + lag) + series)
                for slot in range(max(0, slot_count - lag))
            )
    pairs.extend((3 * slot, 3 * slot + 1) for slot in range(slot_count))
    pairs.extend((3 * slot + 1, 3 * slot + 2) for slot in range(slot_count))
    score = 0.0
    for left, right in pairs:
        observed = math.sqrt(abs(actual[left] - actual[right]))
        expected = sum(
            weight * math.sqrt(abs(vector[left] - vector[right]))
            for vector, weight in zip(vectors, weights, strict=True)
        )
        score += (observed - expected) ** 2
    return score / max(1, len(pairs))


def _daily_block_bootstrap_summary(decisions, samples=10_000):
    """Return a day-weighted mean and paired CI from complete UTC-day blocks."""
    blocks = {}
    for item in decisions:
        delta = item.shadow_regret_eur - item.authoritative_regret_eur
        if not math.isfinite(delta):
            return None, None, None
        blocks.setdefault(item.block_day, []).append(delta)
    daily = [sum(values) / len(values) for values in blocks.values()]
    if len(daily) < 2:
        return (daily[0], None, None) if daily else (None, None, None)
    generator = random.Random(0)
    draws = sorted(
        sum(generator.choice(daily) for _ in daily) / len(daily) for _ in range(samples)
    )
    return (
        sum(daily) / len(daily),
        draws[int(0.025 * samples)],
        draws[int(0.975 * samples)],
    )


def _ev_shape(values, threshold):
    active = [index for index, value in enumerate(values) if value >= threshold]
    if not active:
        return float(len(values)), 0.0
    return float(active[0]), float(len(active))


def _policy_vector(slot) -> tuple[float, float]:
    return float(slot.required_charge_kwh), float(slot.discharge_budget_kwh)


def _serialized_policy(slot) -> tuple[float, float]:
    return float(slot[7]), float(slot[4])


def _policy_distance(left, right) -> float:
    return abs(left[0] - right[0]) + abs(left[1] - right[1])


def _policy_direction(policy) -> int:
    return 1 if policy[0] > 1e-6 else (-1 if policy[1] > 1e-6 else 0)


def _constrained_objective(optimizer, problem, serialized_slot) -> float:
    if len(serialized_slot) < 9:
        return float("inf")
    required_charge = float(serialized_slot[7])
    discharge_budget = float(serialized_slot[4])
    try:
        _plan, diagnostics = optimizer.optimize_with_diagnostics(
            problem,
            required_first_policy=(required_charge, discharge_budget),
        )
        return diagnostics["expected_scenario_cost_eur"]
    except ValueError:
        return float("inf")


def _parse_as_of(value: str | None) -> int:
    if value is None:
        return int(datetime.now(UTC).timestamp() * 1000)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include a timezone")
    return int(parsed.timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", default=DEFAULT_TRACE_DIRECTORY)
    parser.add_argument("--feature-store", default=DEFAULT_FEATURE_STORE)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--decision-stride", type=int, default=4)
    parser.add_argument("--as-of")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    if args.days < 1 or args.decision_stride < 1:
        parser.error("--days and --decision-stride must be positive")
    as_of_ms = _parse_as_of(args.as_of)
    start_ms = as_of_ms - int(timedelta(days=args.days).total_seconds() * 1000)
    traces = load_traces(args.trace_dir, start_ms, as_of_ms)
    actuals = load_actuals(args.feature_store)
    report = summarize(
        traces,
        score_scenarios(traces, actuals, as_of_ms=as_of_ms),
        score_decisions(
            traces,
            actuals,
            as_of_ms=as_of_ms,
            stride=args.decision_stride,
        ),
        score_paths(traces, actuals, as_of_ms=as_of_ms),
    )
    report.update({"as_of_ms": as_of_ms, "window_days": args.days})
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

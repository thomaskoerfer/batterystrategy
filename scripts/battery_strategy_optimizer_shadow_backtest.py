#!/usr/bin/env python3
"""Evaluate stochastic optimizer shadows against finalized actuals."""

from __future__ import annotations

import argparse
import gzip
import json
import math
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

SLOT_MS = 15 * 60 * 1000
BOUNDARY_DECISION_TOLERANCE_MS = 60 * 1000
TRACE_SCHEMA = 2
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
    event_probability: float | None = None
    event_actual: float | None = None


@dataclass(frozen=True, slots=True)
class DecisionScore:
    """One vintage's executable first action against perfect foresight."""

    shadow_error_kwh: float
    authoritative_error_kwh: float
    shadow_direction_match: bool
    authoritative_direction_match: bool
    shadow_regret_eur: float
    authoritative_regret_eur: float


@dataclass(frozen=True, slots=True)
class PathScore:
    """One complete matured multivariate path forecast."""

    energy_score_kwh: float
    variogram_score: float
    ev_start_error_slots: float
    ev_duration_error_slots: float


def load_traces(root: str | Path, start_ms: int, end_ms: int) -> list[dict]:
    """Load complete schema-2 stochastic shadow traces."""
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
        for index, slot in enumerate(trace.get("load", {}).get("slots", ())):
            start_ms, end_ms = int(slot[0]), int(slot[1])
            if start_ms < int(trace["generated_at_ms"]):
                continue
            actual = _usable_actual(actuals.get(start_ms), end_ms, as_of_ms)
            if actual is None or not paths:
                continue
            probabilities = [float(path["probability"]) for path in paths]
            for series, position, actual_key in (
                ("load", 1, "house_load_no_ev_kwh"),
                ("pv", 2, "pv_generation_kwh"),
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
                    ev_probability,
                    ev_actual,
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
        perfect_action = _plan_action(perfect.slots[0])
        shadow_action = _serialized_action(shadow["slots"][0])
        authoritative_action = _serialized_action(authoritative["slots"][0])
        optimum = perfect_diagnostics["expected_scenario_cost_eur"]
        shadow_cost = _constrained_objective(optimizer, problem, shadow["slots"][0])
        authoritative_cost = _constrained_objective(
            optimizer, problem, authoritative["slots"][0]
        )
        scores.append(
            DecisionScore(
                abs(shadow_action - perfect_action),
                abs(authoritative_action - perfect_action),
                _direction(shadow_action) == _direction(perfect_action),
                _direction(authoritative_action) == _direction(perfect_action),
                max(0.0, shadow_cost - optimum),
                max(0.0, authoritative_cost - optimum),
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
                if int(row[0]) >= int(trace["generated_at_ms"])
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
        ev_policy = (trace.get("optimization_problem") or {}).get("ev_policy") or {}
        threshold = float(ev_policy.get("ev_active_threshold_w", 300.0)) / 4000.0
        actual_start, actual_duration = _ev_shape(
            [float(item.get("ev_charge_kwh", 0.0)) for item in matured], threshold
        )
        expected_start = 0.0
        expected_duration = 0.0
        for probability, path in zip(probabilities, paths, strict=True):
            start, duration = _ev_shape(
                [float(row[3]) for row in path["slots"][first:]], threshold
            )
            expected_start += probability * start
            expected_duration += probability * duration
        scores.append(
            PathScore(
                _energy_score(scenario_vectors, probabilities, actual_vector),
                _variogram_score(scenario_vectors, probabilities, actual_vector),
                abs(expected_start - actual_start),
                abs(expected_duration - actual_duration),
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
        }
    ev = [item for item in scenario_scores if item.series == "ev_event"]
    runtimes = [
        float(item["shadow_evaluation"]["runtime_ms"])
        for item in traces
        if (item.get("shadow_evaluation") or {}).get("runtime_ms") is not None
    ]
    paths = path_scores or []
    return {
        "non_authoritative": True,
        "trace_vintages": len(traces),
        "completed_shadow_vintages": sum(
            (item.get("shadow_evaluation") or {}).get("status") == "completed"
            for item in traces
        ),
        "scenario_metrics": scenario_metrics,
        "ev_event_samples": len(ev),
        "ev_event_brier_score": (
            sum((item.event_probability - item.event_actual) ** 2 for item in ev)
            / len(ev)
            if ev
            else None
        ),
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
        "complete_path_vintages": len(paths),
        "mean_energy_score_kwh": (
            sum(item.energy_score_kwh for item in paths) / len(paths) if paths else None
        ),
        "mean_variogram_score": (
            sum(item.variogram_score for item in paths) / len(paths) if paths else None
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
        "shadow_runtime_mean_ms": sum(runtimes) / len(runtimes) if runtimes else None,
        "shadow_runtime_max_ms": max(runtimes) if runtimes else None,
    }


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
    # Adjacent elements cover within-slot cross-series and between-slot temporal
    # dependence without a quadratic all-pairs cost on long planning horizons.
    score = 0.0
    for index in range(len(actual) - 1):
        observed = math.sqrt(abs(actual[index] - actual[index + 1]))
        expected = sum(
            weight * math.sqrt(abs(vector[index] - vector[index + 1]))
            for vector, weight in zip(vectors, weights, strict=True)
        )
        score += (observed - expected) ** 2
    return score / max(1, len(actual) - 1)


def _ev_shape(values, threshold):
    active = [index for index, value in enumerate(values) if value >= threshold]
    if not active:
        return float(len(values)), 0.0
    return float(active[0]), float(len(active))


def _plan_action(slot) -> float:
    return float(slot.planned_charge_kwh) - float(slot.planned_discharge_kwh)


def _serialized_action(slot) -> float:
    return float(slot[2]) - float(slot[3])


def _constrained_objective(optimizer, problem, serialized_slot) -> float:
    capacity = float(problem.constraints.capacity_kwh)
    required_end = float(serialized_slot[6]) * capacity / 100.0
    try:
        _plan, diagnostics = optimizer.optimize_with_diagnostics(
            problem, required_first_end_kwh=required_end
        )
        return diagnostics["expected_scenario_cost_eur"]
    except ValueError:
        return float("inf")


def _direction(value: float) -> int:
    return 1 if value > 1e-6 else (-1 if value < -1e-6 else 0)


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

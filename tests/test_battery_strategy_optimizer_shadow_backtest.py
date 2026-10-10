import gzip
import importlib.util
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "scripts" / "battery_strategy_optimizer_shadow_backtest.py"
spec = importlib.util.spec_from_file_location(
    "battery_strategy_optimizer_shadow_backtest", MODULE_PATH
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_trace_loader_accepts_every_produced_optimizer_envelope(tmp_path):
    day = tmp_path / "2026-09-25"
    day.mkdir()
    payloads = [
        {
            "schema_version": schema,
            "non_authoritative": True,
            "generated_at_ms": schema,
            "optimizer_plans" if schema in {4, 7} else "optimizer_plan": {},
        }
        for schema in range(4, 9)
    ]
    payloads.insert(
        4,
        {
            "schema_version": 7,
            "non_authoritative": True,
            "generated_at_ms": 70,
            "optimizer_plan": {},
        },
    )
    for index, payload in enumerate(payloads):
        path = day / f"{index}.json.gz"
        path.write_bytes(gzip.compress(json.dumps(payload).encode()))

    assert mod.load_traces(tmp_path, 0, 100) == payloads


def test_release_cohort_keeps_only_checked_out_implementation(monkeypatch):
    def trace(generated, optimizer, scenario, status="completed"):
        return {
            "schema_version": 7,
            "generated_at_ms": generated,
            "load": {"model_version": "load-v1"},
            "pv": {"model_version": "pv-v1"},
            "ev": {"model_version": "ev-v1"},
            "scenarios": {"model_version": scenario},
            "shadow_evaluation": {
                "status": status,
                "optimizer_version": optimizer,
                "scenario_model_version": scenario,
                "decision": {"optimizer_version": optimizer},
                "scenario_build": {"model_version": scenario},
            },
        }

    old = trace(1, "scenario-dp-v2", "scenario-v1")
    current = trace(2, "scenario-dp-v3", "scenario-v1")
    failed_newer = trace(3, "scenario-dp-v4", "scenario-v2", "optimizer_failed")
    monkeypatch.setattr(mod, "UNIFIED_OPTIMIZER_VERSION", "scenario-dp-v3")
    monkeypatch.setattr(mod, "SCENARIO_RELEASE_VERSION", "scenario-v1+repair-v1")
    current["shadow_evaluation"]["scenario_build"]["model_version"] = (
        "scenario-v1+repair-v1"
    )
    current["shadow_evaluation"]["scenario_model_version"] = "scenario-v1+repair-v1"

    cohort = mod.current_optimizer_release_cohort([old, current, failed_newer])

    assert cohort == (
        7,
        "scenario-dp-v3",
        "scenario-v1+repair-v1",
        "load-v1",
        "pv-v1",
        "ev-v1",
    )
    assert mod.select_optimizer_release_cohort(
        [old, current, failed_newer], cohort
    ) == [current]
    assert mod.current_optimizer_release_cohort([old, failed_newer]) is None


def test_release_cohort_does_not_mix_forecast_model_versions(monkeypatch):
    monkeypatch.setattr(mod, "UNIFIED_OPTIMIZER_VERSION", "optimizer-v1")
    monkeypatch.setattr(mod, "SCENARIO_RELEASE_VERSION", "scenario-v1")

    def trace(generated, load_version):
        return {
            "schema_version": 7,
            "generated_at_ms": generated,
            "load": {"model_version": load_version},
            "pv": {"model_version": "pv-v1"},
            "ev": {"model_version": "ev-v1"},
            "shadow_evaluation": {
                "status": "completed",
                "optimizer_version": "optimizer-v1",
                "scenario_model_version": "scenario-v1",
            },
        }

    old = trace(1, "load-old")
    current = trace(2, "load-current")
    cohort = mod.current_optimizer_release_cohort([old, current])

    assert cohort is not None
    assert cohort[3] == "load-current"
    assert mod.select_optimizer_release_cohort([old, current], cohort) == [current]


def test_serialized_policy_normalizes_float_noise_to_zero():
    slot = [0, "idle", 0.0, 0.0, -0.0, 50.0, 50.0, 2e-16, 0.0]

    assert mod._serialized_policy(slot) == (0.0, 0.0)


def test_constrained_replay_uses_normalized_policy(monkeypatch):
    captured = {}

    class Optimizer:
        def optimize_with_diagnostics(self, _problem, *, required_first_policy):
            captured["policy"] = required_first_policy
            return None, {"expected_scenario_cost_eur": 0.0}

    slot = [0, "idle", 0.0, 0.0, -0.0, 50.0, 50.0, 2e-16, 0.0]

    assert mod._constrained_objective(Optimizer(), object(), slot) == 0.0
    assert captured["policy"] == (0.0, 0.0)


def test_scenario_scores_report_crps_coverage_and_ev_brier():
    trace = {
        "generated_at_ms": 0,
        "load": {"slots": [[900_000, 1_800_000, 0.2]]},
        "pv": {"slots": [[900_000, 1_800_000, 0.2]]},
        "optimization_problem": {"ev_policy": {"ev_active_threshold_w": 300.0}},
        "scenarios": {
            "paths": [
                {"probability": 0.5, "slots": [[900_000, 0.1, 0.3, 0.0]]},
                {"probability": 0.5, "slots": [[900_000, 0.3, 0.1, 0.2]]},
            ]
        },
    }
    actuals = {
        900_000: {
            "coverage": 1.0,
            "flags": [],
            "house_load_no_ev_kwh": 0.2,
            "pv_generation_kwh": 0.2,
            "ev_charge_kwh": 0.2,
        }
    }

    scores = mod.score_scenarios([trace], actuals, as_of_ms=1_800_000)
    summary = mod.summarize([], scores, [])

    assert summary["scenario_metrics"]["load"]["mean_crps_kwh"] == pytest.approx(0.05)
    assert summary["scenario_metrics"]["pv"]["p10_p90_coverage_pct"] == 100.0
    assert summary["ev_event_brier_score"] == pytest.approx(0.25)


def test_ev_event_score_uses_trace_threshold():
    trace = {
        "generated_at_ms": 0,
        "load": {"slots": [[900_000, 1_800_000, 0.1]]},
        "pv": {"slots": [[900_000, 1_800_000, 0.1]]},
        "optimization_problem": {"ev_policy": {"ev_active_threshold_w": 1000.0}},
        "scenarios": {
            "paths": [{"probability": 1.0, "slots": [[900_000, 0.1, 0.1, 0.2]]}]
        },
    }
    actuals = {
        900_000: {
            "coverage": 1.0,
            "flags": [],
            "house_load_no_ev_kwh": 0.1,
            "pv_generation_kwh": 0.1,
            "ev_charge_kwh": 0.2,
        }
    }

    summary = mod.summarize(
        [trace], mod.score_scenarios([trace], actuals, as_of_ms=1_800_000), []
    )

    assert summary["ev_event_brier_score"] == 0.0


def test_shadow_summary_compares_actions_with_perfect_foresight():
    decisions = [mod.DecisionScore(0.05, 0.2, True, False, 0.01, 0.04)]

    summary = mod.summarize(
        [{"shadow_evaluation": {"status": "completed", "runtime_ms": 12.0}}],
        [],
        decisions,
    )

    assert summary["completed_shadow_vintages"] == 1
    assert summary["shadow_first_action_mae_kwh"] == pytest.approx(0.05)
    assert summary["authoritative_first_action_mae_kwh"] == pytest.approx(0.2)
    assert summary["shadow_direction_match_pct"] == 100.0
    assert summary["shadow_mean_perfect_foresight_regret_eur"] == pytest.approx(0.01)
    assert summary["shadow_runtime_max_ms"] == 12.0


def test_scenario_success_rate_uses_pre_registered_evaluation_vintages():
    completed = {
        "generated_at_ms": 0,
        "shadow_evaluation": {
            "status": "completed",
            "runtime_ms": 1.0,
            "scenario_build": {"eligible": True, "status": "completed"},
        },
    }
    failed_but_not_sampled = {
        "generated_at_ms": mod.SLOT_MS,
        "shadow_evaluation": {
            "status": "insufficient_evidence",
            "runtime_ms": 1.0,
            "scenario_build": {
                "eligible": True,
                "status": "insufficient_evidence",
            },
        },
    }

    report = mod.summarize(
        [completed, failed_but_not_sampled],
        [],
        [],
        evaluation_traces=[completed],
    )

    assert report["eligible_scenario_vintages"] == 1
    assert report["scenario_generation_success_pct"] == 100.0


def test_scenario_builder_exception_counts_as_eligible_failure():
    completed = {
        "generated_at_ms": 0,
        "shadow_evaluation": {
            "status": "completed",
            "runtime_ms": 1.0,
            "scenario_build": {"eligible": True, "status": "completed"},
        },
    }
    failed = {
        "generated_at_ms": mod.SLOT_MS,
        "shadow_evaluation": {
            "status": "scenario_builder_failed",
            "runtime_ms": 1.0,
            "optimizer_version": "scenario-dp-v3",
            "scenario_model_version": "scenario-v1+repair-v1",
        },
    }

    report = mod.summarize([completed, failed], [], [])

    assert report["eligible_scenario_vintages"] == 2
    assert report["scenario_generation_success_pct"] == 50.0


def test_optimizer_exception_reduces_release_success_rate():
    completed = {
        "generated_at_ms": 0,
        "shadow_evaluation": {"status": "completed", "runtime_ms": 1.0},
    }
    failed = {
        "generated_at_ms": mod.SLOT_MS,
        "shadow_evaluation": {"status": "optimizer_failed", "runtime_ms": 1.0},
    }

    report = mod.summarize([completed, failed], [], [])

    assert report["optimizer_evaluation_vintages"] == 2
    assert report["optimizer_success_pct"] == 50.0
    assert report["release_gate"]["minimum_optimizer_success_pct"] == 99.0


def test_complete_path_scores_joint_shape_and_ev_timing():
    trace = {
        "generated_at_ms": 0,
        "load": {"slots": [[0, 900_000, 0.2], [900_000, 1_800_000, 0.1]]},
        "pv": {"slots": [[0, 900_000, 0.0], [900_000, 1_800_000, 0.3]]},
        "optimization_problem": {"ev_policy": {"ev_active_threshold_w": 300.0}},
        "scenarios": {
            "paths": [
                {
                    "probability": 1.0,
                    "slots": [
                        [0, 0.2, 0.0, 0.0],
                        [900_000, 0.1, 0.3, 0.2],
                    ],
                }
            ]
        },
    }
    actuals = {
        0: {
            "coverage": 1.0,
            "flags": [],
            "house_load_no_ev_kwh": 0.2,
            "pv_generation_kwh": 0.0,
            "ev_charge_kwh": 0.0,
        },
        900_000: {
            "coverage": 1.0,
            "flags": [],
            "house_load_no_ev_kwh": 0.1,
            "pv_generation_kwh": 0.3,
            "ev_charge_kwh": 0.2,
        },
    }

    scores = mod.score_paths([trace], actuals, as_of_ms=1_800_000)
    summary = mod.summarize([], [], [], scores)

    assert scores[0].energy_score_kwh == 0.0
    assert scores[0].variogram_score == 0.0
    assert scores[0].ev_start_error_slots == 0.0
    assert scores[0].ev_duration_error_slots == 0.0
    assert summary["complete_path_vintages"] == 1
    assert summary["mean_energy_score_kwh"] == 0.0


def test_decision_scores_ignore_mid_slot_vintages():
    trace = {
        "generated_at_ms": mod.BOUNDARY_DECISION_TOLERANCE_MS + 1,
        "load": {"slots": [[0, 900_000]]},
        "optimizer_plans": {
            "shadow": {"slots": [[0, 900_000, 0, 0, 0, 0, 50]]},
            "authoritative": {"slots": [[0, 900_000, 0, 0, 0, 0, 50]]},
        },
        "optimization_problem": {},
    }

    assert mod.score_decisions([trace], {}, as_of_ms=900_000) == []


def test_hourly_boundary_sampling_keeps_first_vintage_with_scheduler_latency():
    def trace(generated_at_ms, slot_start_ms):
        return {
            "generated_at_ms": generated_at_ms,
            "load": {"slots": [[slot_start_ms, slot_start_ms + mod.SLOT_MS]]},
        }

    traces = [
        trace(10_000, 0),
        trace(mod.SLOT_MS + 8_000, mod.SLOT_MS),
        trace(4 * mod.SLOT_MS + 30_000, 4 * mod.SLOT_MS),
        trace(8 * mod.SLOT_MS + 30_001, 8 * mod.SLOT_MS),
    ]

    selected = mod.hourly_boundary_vintages(traces)

    assert [item["generated_at_ms"] for item in selected] == [
        10_000,
        4 * mod.SLOT_MS + 30_000,
    ]


def test_perfect_foresight_removes_elapsed_first_slot_energy():
    trace = {
        "generated_at_ms": 30_000,
        "load": {
            "generated_at_ms": 30_000,
            "slots": [[0, mod.SLOT_MS, 0.0]],
        },
        "pv": {
            "generated_at_ms": 30_000,
            "slots": [[0, mod.SLOT_MS, 0.0]],
        },
        "optimization_problem": {
            "problem_id": "problem",
            "as_of_ms": 30_000,
            "battery": {"captured_at_ms": 30_000, "soc_pct": 50.0},
            "constraints": {
                "capacity_kwh": 6.0,
                "min_soc_pct": 5.0,
                "max_soc_pct": 100.0,
                "max_charge_power_w": 2400.0,
                "max_discharge_power_w": 2400.0,
                "round_trip_efficiency": 0.8,
            },
            "commercial_policy": {"min_margin_ct_per_kwh": 2.0},
            "ev_policy": {},
            "market": [[0, 30.0, 0.0, "captured"]],
        },
    }
    actuals = {
        0: {
            "coverage": 1.0,
            "flags": [],
            "house_load_no_ev_kwh": 0.9,
            "pv_generation_kwh": 0.45,
            "ev_charge_kwh": 0.18,
        }
    }

    problem = mod._perfect_foresight_problem(trace, actuals, as_of_ms=mod.SLOT_MS)

    assert problem is not None
    remaining = (mod.SLOT_MS - 30_000) / mod.SLOT_MS
    path = problem.scenarios.scenarios[0]
    assert path.slots[0].house_load_kwh == pytest.approx(0.9 * remaining)
    assert path.slots[0].pv_generation_kwh == pytest.approx(0.45 * remaining)
    assert path.slots[0].ev_charge_kwh == pytest.approx(0.18 * remaining)


def test_daily_bootstrap_point_estimate_uses_same_day_weighting_as_ci():
    decisions = [
        mod.DecisionScore(0, 0, True, True, 1.0, 0.0, block_day=1),
        *(
            mod.DecisionScore(0, 0, True, True, 0.0, 1.0, block_day=2)
            for _ in range(10)
        ),
    ]

    mean, low, high = mod._daily_block_bootstrap_summary(decisions, samples=1000)

    assert mean == pytest.approx(0.0)
    assert low is not None
    assert high is not None


def test_release_gate_requires_complete_days_and_real_ev_sessions():
    traces = [
        {
            "generated_at_ms": day * mod.DAY_MS,
            "shadow_evaluation": {"status": "completed", "runtime_ms": 1.0},
        }
        for day in range(7)
    ]

    report = mod.summarize(traces, [], [], [])

    assert report["release_gate"]["status"] == "insufficient_data"
    assert report["release_gate"]["complete_observation_days"] == 0
    assert report["matured_ev_sessions"] == 0


def test_regret_gate_excludes_decisions_from_incomplete_utc_days():
    traces = [
        {
            "generated_at_ms": slot * mod.SLOT_MS,
            "shadow_evaluation": {"status": "completed", "runtime_ms": 1.0},
        }
        for slot in range(87)
    ]
    traces.append(
        {
            "generated_at_ms": mod.DAY_MS,
            "shadow_evaluation": {"status": "completed", "runtime_ms": 1.0},
        }
    )
    decisions = [
        mod.DecisionScore(0, 0, True, True, 1.0, 0.0, block_day=0),
        mod.DecisionScore(0, 0, True, True, 0.0, 100.0, block_day=1),
    ]

    report = mod.summarize(traces, [], decisions, [])

    assert report["eligible_perfect_foresight_vintages"] == 1
    assert report["paired_regret_delta_mean_eur"] == pytest.approx(1.0)

import importlib.util
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


def test_complete_path_scores_joint_shape_and_ev_timing():
    trace = {
        "generated_at_ms": 0,
        "load": {"slots": [[0, 900_000], [900_000, 1_800_000]]},
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

    assert scores == [mod.PathScore(0.0, 0.0, 0.0, 0.0)]
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

import gzip
import importlib.util
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "scripts" / "battery_strategy_forecast_backtest.py"
spec = importlib.util.spec_from_file_location(
    "battery_strategy_forecast_backtest", MODULE_PATH
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def _write_trace(root, *, generated_at_ms, slots):
    day = root / "2027-01-20"
    day.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "non_authoritative": True,
        "generated_at_ms": generated_at_ms,
        "load": {
            "model_version": "load-v2",
            "slots": [
                [start, start + mod.SLOT_MS, load, None, None, 0, 1.0, []]
                for start, load, _pv, _component in slots
            ],
            "components": [
                {
                    "component_key": "heat_pump_dhw",
                    "model_version": "dhw-v1",
                    "slots": [
                        [
                            start,
                            start + mod.SLOT_MS,
                            component,
                            None,
                            None,
                            0,
                            1.0,
                            [],
                        ]
                        for start, _load, _pv, component in slots
                    ],
                }
            ],
        },
        "pv": {
            "model_version": "pv-v2",
            "slots": [
                [start, start + mod.SLOT_MS, pv, None, None, 0, 1.0, []]
                for start, _load, pv, _component in slots
            ],
        },
    }
    path = day / f"{generated_at_ms}.json.gz"
    path.write_bytes(gzip.compress(json.dumps(payload).encode()))


def _actual(start_ms, *, load=0.2, pv=0.1, flags=None, component_flags=None):
    return {
        "start_ms": start_ms,
        "house_load_no_ev_kwh": load,
        "pv_generation_kwh": pv,
        "ev_charge_kwh": 9.0,
        "coverage": 1.0,
        "flags": flags or [],
        "load_components": [
            {
                "key": "heat_pump_dhw",
                "energy_kwh": 0.05,
                "coverage": 1.0,
                "flags": component_flags or [],
            }
        ],
    }


def test_evaluator_scores_matured_future_slots_by_series_and_lead(tmp_path):
    generated = 1_800_000_000_000
    current_slot = generated // mod.SLOT_MS * mod.SLOT_MS
    future_slot = current_slot + mod.SLOT_MS
    later_slot = current_slot + 2 * mod.SLOT_MS
    _write_trace(
        tmp_path,
        generated_at_ms=generated,
        slots=[
            (current_slot, 9.0, 9.0, 9.0),
            (future_slot, 0.3, 0.2, 0.08),
            (later_slot, 0.1, 0.0, 0.04),
        ],
    )
    actuals = {
        future_slot: _actual(future_slot, load=0.2, pv=0.1),
        later_slot: _actual(later_slot, load=0.2, pv=0.0),
    }

    observations = mod.load_forecast_observations(tmp_path)
    residuals = mod.compare_forecasts(
        observations,
        actuals,
        as_of_ms=later_slot + mod.SLOT_MS,
    )
    metrics = mod.summarize(residuals)

    assert all(item.start_ms != current_slot for item in residuals)
    assert {item.series for item in residuals} == {
        "load_total",
        "pv",
        "load_component:heat_pump_dhw",
    }
    load = next(row for row in metrics if row["series"] == "load_total")
    assert load["samples"] == 2
    assert load["mae_kwh"] == pytest.approx(0.1)
    assert load["bias_kwh"] == pytest.approx(0.0)
    assert load["actual_kwh"] == pytest.approx(0.4)
    assert load["forecast_quality_coverage"] == 1.0
    assert load["p10_p90_samples"] == 0
    # EV is deliberately not part of the selected whole-house actual.
    assert load["actual_kwh"] != 18.4


def test_evaluator_excludes_unfinished_missing_and_flagged_actuals(tmp_path):
    generated = 1_800_000_000_000
    first = (generated // mod.SLOT_MS + 1) * mod.SLOT_MS
    second = first + mod.SLOT_MS
    third = second + mod.SLOT_MS
    _write_trace(
        tmp_path,
        generated_at_ms=generated,
        slots=[
            (first, 0.2, 0.1, 0.05),
            (second, 0.2, 0.1, 0.05),
            (third, 0.2, 0.1, 0.05),
        ],
    )
    actuals = {
        first: _actual(first, flags=["restart_gap"]),
        second: _actual(second, component_flags=["missing_component"]),
        third: _actual(third),
    }

    residuals = mod.compare_forecasts(
        mod.load_forecast_observations(tmp_path),
        actuals,
        as_of_ms=third + mod.SLOT_MS - 1,
    )

    # First is invalid globally; third is not complete. The second total and PV
    # remain usable, while its independently flagged component is excluded.
    assert {(item.series, item.start_ms) for item in residuals} == {
        ("load_total", second),
        ("pv", second),
    }


def test_summary_reports_calibrated_interval_coverage():
    rows = mod.summarize(
        [
            mod.Residual(
                "pv",
                "pv-v2",
                "1-6h",
                0,
                mod.SLOT_MS,
                0.2,
                0.18,
                0.1,
                0.3,
                0.8,
            ),
            mod.Residual(
                "pv",
                "pv-v2",
                "1-6h",
                0,
                mod.SLOT_MS * 2,
                0.2,
                0.4,
                0.1,
                0.3,
                1.0,
            ),
        ]
    )

    assert rows[0]["p10_p90_samples"] == 2
    assert rows[0]["p10_p90_coverage_pct"] == 50.0
    assert rows[0]["p10_p90_mean_width_kwh"] == pytest.approx(0.2)
    assert rows[0]["forecast_quality_coverage"] == 0.9


def test_loader_uses_independent_series_generation_times(tmp_path):
    generated = 1_800_000_000_000
    slot = (generated // mod.SLOT_MS + 2) * mod.SLOT_MS
    _write_trace(tmp_path, generated_at_ms=generated, slots=[(slot, 0.2, 0.1, 0.05)])
    path = next(tmp_path.rglob("*.json.gz"))
    payload = json.loads(gzip.decompress(path.read_bytes()))
    payload["load"]["generated_at_ms"] = generated - 60_000
    payload["pv"]["generated_at_ms"] = generated + 60_000
    path.write_bytes(gzip.compress(json.dumps(payload).encode()))

    observations = mod.load_forecast_observations(tmp_path)

    load = next(item for item in observations if item.series == "load_total")
    component = next(
        item for item in observations if item.series.startswith("load_component:")
    )
    pv = next(item for item in observations if item.series == "pv")
    assert load.generated_at_ms == generated - 60_000
    assert component.generated_at_ms == generated - 60_000
    assert pv.generated_at_ms == generated + 60_000


def test_loader_rejects_unknown_trace_and_feature_store_schemas(tmp_path):
    generated = 1_800_000_000_000
    slot = (generated // mod.SLOT_MS + 1) * mod.SLOT_MS
    _write_trace(tmp_path, generated_at_ms=generated, slots=[(slot, 0.2, 0.1, 0.05)])
    trace_path = next(tmp_path.rglob("*.json.gz"))
    trace_payload = json.loads(gzip.decompress(trace_path.read_bytes()))
    trace_payload["schema_version"] = 99
    trace_path.write_bytes(gzip.compress(json.dumps(trace_payload).encode()))
    feature_path = tmp_path / "features.json.gz"
    feature_path.write_bytes(
        gzip.compress(json.dumps({"schema_version": 99, "slots": []}).encode())
    )

    assert mod.load_forecast_observations(tmp_path) == []
    with pytest.raises(ValueError, match="unsupported feature-store schema"):
        mod.load_actuals(feature_path)


def test_loader_scores_whole_heat_pump_shadow_separately(tmp_path):
    generated = 1_800_000_000_000
    slot = (generated // mod.SLOT_MS + 1) * mod.SLOT_MS
    _write_trace(tmp_path, generated_at_ms=generated, slots=[(slot, 0.3, 0.0, 0.1)])
    path = next(tmp_path.rglob("*.json.gz"))
    payload = json.loads(gzip.decompress(path.read_bytes()))
    payload["schema_version"] = 7
    forecast_slot = [slot, slot + mod.SLOT_MS, 0.25, 0.1, 0.4, 12, 1.0, []]
    payload["forecast_shadows"] = {
        "heat_pump": {
            "status": "completed",
            "forecast": {
                "generated_at_ms": generated,
                "diagnostics": {"status": "cold_start"},
                "total": {"model_version": "wp-shadow-v1", "slots": [forecast_slot]},
                "components": [
                    {
                        "component_key": "heat_pump_space_heating",
                        "model_version": "heating-shadow-v1",
                        "slots": [forecast_slot],
                    }
                ],
            },
        }
    }
    path.write_bytes(gzip.compress(json.dumps(payload).encode()))
    actual = _actual(slot)
    actual["load_components"].append(
        {
            "key": "heat_pump_space_heating",
            "energy_kwh": 0.2,
            "coverage": 1.0,
            "flags": [],
        }
    )

    residuals = mod.compare_forecasts(
        mod.load_forecast_observations(tmp_path),
        {slot: actual},
        as_of_ms=slot + mod.SLOT_MS,
    )

    shadow = {
        item.series: item for item in residuals if item.series.startswith("shadow")
    }
    assert shadow["shadow_heat_pump_total"].actual_kwh == pytest.approx(0.25)
    assert shadow["shadow_load_component:heat_pump_space_heating"].actual_kwh == 0.2
    assert mod.load_heat_pump_trace_statuses(tmp_path)[0].status == "cold_start"


def test_heat_pump_gate_requires_paired_improvement_and_complete_days():
    residuals = []
    statuses = []
    actuals = {}
    for index in range(7 * 96):
        start_ms = index * mod.SLOT_MS
        generated_at_ms = start_ms
        statuses.append(mod.HeatPumpTraceStatus(generated_at_ms, start_ms, "ready"))
        for series, forecast, actual in (
            ("load_component:heat_pump_dhw", 0.1, 0.1),
            ("shadow_load_component:heat_pump_dhw", 0.1, 0.1),
            ("load_component:heat_pump_space_heating", 0.2, 0.1),
            ("shadow_heat_pump_total", 0.22, 0.2),
        ):
            residuals.append(
                mod.Residual(
                    series,
                    "model-v1",
                    "0-1h",
                    generated_at_ms,
                    start_ms,
                    forecast,
                    actual,
                    None,
                    None,
                    1.0,
                )
            )
        active = index in {1, 3, 5}
        actuals[start_ms] = {
            "coverage": 1.0,
            "flags": [],
            "load_components": [
                {
                    "key": "heat_pump_dhw",
                    "energy_kwh": 0.1 if active else 0.0,
                    "coverage": 1.0,
                    "flags": [],
                },
                {
                    "key": "heat_pump_space_heating",
                    "energy_kwh": 0.1 if active else 0.0,
                    "coverage": 1.0,
                    "flags": [],
                },
            ],
        }

    report = mod.evaluate_heat_pump_gate(residuals, statuses, actuals, timezone="UTC")

    assert report["verdict"] == "pass"
    assert report["evidence"]["complete_local_days"] == 7
    assert report["evidence"]["space_heating_runs"] == 3
    assert report["evidence"]["dhw_cycles"] == 3
    assert (
        report["paired_metrics"]["shadow_mae_kwh"]
        < report["paired_metrics"]["authoritative_mae_kwh"]
    )


def test_heat_pump_gate_reports_insufficient_data_instead_of_passing_early():
    statuses = [
        mod.HeatPumpTraceStatus(
            index * mod.SLOT_MS, index * mod.SLOT_MS, "not_configured"
        )
        for index in range(7 * 96)
    ]

    report = mod.evaluate_heat_pump_gate([], statuses, {}, timezone="UTC")

    assert report["verdict"] == "insufficient_data"
    assert report["evidence"]["complete_local_days"] == 0
    assert report["evidence"]["candidate_status_counts"] == {"not_configured": 672}

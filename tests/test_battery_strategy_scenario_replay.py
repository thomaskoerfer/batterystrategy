import datetime as dt
import gzip
import importlib.util
import json
import pathlib
import sys

from custom_components.battery_strategy.contracts import (
    DataQuality,
    EvForecast,
    EvForecastSlot,
    ForecastDistributionBundle,
    ForecastSlot,
    HistoricalFeatureSlot,
    LoadForecast,
    PvForecast,
    QuantileEnergy,
    ResidualCohort,
    ScenarioBuildRequest,
    ScenarioBuildStatus,
    ScenarioEvidenceSnapshot,
    ScenarioGenerationSettings,
    SlotKey,
)
from custom_components.battery_strategy.forecast_trace import append_forecast_trace
from custom_components.battery_strategy.scenario_generation import ScenarioBuilder

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "scripts" / "battery_strategy_scenario_replay.py"
spec = importlib.util.spec_from_file_location(
    "battery_strategy_scenario_replay", MODULE_PATH
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

SLOT_MS = 15 * 60 * 1000


def _fixture():
    start = dt.datetime(2027, 1, 20, 10, 0, tzinfo=dt.UTC)
    start_ms = int(start.timestamp() * 1000)
    slot = SlotKey(start_ms, start_ms + SLOT_MS)
    bundle = ForecastDistributionBundle(
        LoadForecast(
            "load",
            start_ms,
            start_ms,
            "load-v1",
            (ForecastSlot(slot, QuantileEnergy(0.4, 0.2, 0.6, 12)),),
        ),
        PvForecast(
            "pv",
            start_ms,
            start_ms,
            "pv-v1",
            (ForecastSlot(slot, QuantileEnergy(0.2, 0.0, 0.4, 12)),),
        ),
        EvForecast(
            "ev",
            start_ms,
            start_ms,
            "ev-v1",
            (EvForecastSlot(slot, QuantileEnergy(0.0, 0.0, 0.4, 12), 0.2, 0.1),),
        ),
    )
    history = tuple(
        HistoricalFeatureSlot(
            SlotKey(
                start_ms - weeks * 7 * 24 * 60 * 60 * 1000,
                start_ms - weeks * 7 * 24 * 60 * 60 * 1000 + SLOT_MS,
            ),
            0.2 + weeks / 10,
            0.1 + weeks / 20,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            None,
            DataQuality(),
        )
        for weeks in (1, 2, 3, 4)
    )
    request = ScenarioBuildRequest(
        bundle,
        ScenarioEvidenceSnapshot(
            "evidence",
            start_ms,
            start_ms,
            "UTC",
            history,
            (
                ResidualCohort(
                    "@total_load\x1fload-v1\x1f0\x1f*\x1f*",
                    tuple((float(index) - 6.0) / 100 for index in range(12)),
                ),
                ResidualCohort(
                    "component:dishwasher\x1fdishwasher-v1\x1f0\x1f*\x1f*",
                    tuple(float(index) / 100 for index in range(12)),
                ),
            ),
            0.5,
            0.075,
        ),
        ScenarioGenerationSettings(2, 4),
    )
    return bundle, history, request


def test_recorded_replay_rebuilds_scenarios_from_trace_and_feature_history(tmp_path):
    bundle, history, request = _fixture()
    path = append_forecast_trace(tmp_path, bundle, scenario_request=request)
    trace = json.loads(gzip.decompress(path.read_bytes()))

    fidelity, result = mod.rebuild_trace(trace, history)

    expected = ScenarioBuilder().build(request)

    assert fidelity == "reconstructed_from_feature_store"
    assert result.status is ScenarioBuildStatus.COMPLETED
    assert result.scenarios is not None
    assert len(result.scenarios.scenarios) == 4
    assert result.scenarios == expected.scenarios
    assert (
        result.diagnostics.input_fingerprint == expected.diagnostics.input_fingerprint
    )


def test_legacy_replay_is_explicitly_marked_as_quantile_fallback(tmp_path):
    bundle, history, request = _fixture()
    path = append_forecast_trace(tmp_path, bundle, scenario_request=request)
    trace = json.loads(gzip.decompress(path.read_bytes()))
    trace.pop("scenario_replay")

    fidelity, result = mod.rebuild_trace(
        trace,
        history,
        fallback_timezone="UTC",
        fallback_pv_slot_cap_kwh=0.5,
    )

    assert fidelity == "quantile_fallback"
    assert result.status is ScenarioBuildStatus.COMPLETED

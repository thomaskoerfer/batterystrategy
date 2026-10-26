#!/usr/bin/env python3
"""Rebuild Scenario Builder outputs from local immutable trace artifacts."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from custom_components.battery_strategy.contracts import (
    DataQuality,
    EvForecast,
    EvForecastSlot,
    ForecastDistributionBundle,
    ForecastSlot,
    LoadForecast,
    LoadForecastComponent,
    PvForecast,
    QualityFlag,
    QuantileEnergy,
    ResidualCohort,
    ScenarioBuildRequest,
    ScenarioEvidenceSnapshot,
    ScenarioGenerationSettings,
    ScenarioMarketSlot,
    SlotKey,
)
from custom_components.battery_strategy.feature_store import CompressedFeatureStore
from custom_components.battery_strategy.scenario_generation import ScenarioBuilder

SLOT_H = 0.25


def rebuild_trace(
    trace: dict,
    history,
    *,
    fallback_timezone: str | None = None,
    fallback_pv_slot_cap_kwh: float | None = None,
):
    """Rebuild one vintage and report whether its evidence is exact."""
    bundle, components_complete = _bundle(trace)
    replay = trace.get("scenario_replay")
    if isinstance(replay, dict):
        evidence_payload = replay["evidence"]
        fidelity = (
            "reconstructed_from_feature_store"
            if components_complete and not trace.get("truncated", False)
            else "reconstructed_without_complete_components"
        )
        residuals = tuple(
            ResidualCohort(str(key), tuple(float(value) for value in values))
            for key, values in evidence_payload.get("residual_cohorts", ())
        )
        settings = ScenarioGenerationSettings(**replay.get("settings", {}))
        market = _market_from_replay(replay.get("market", ()))
        timezone = str(evidence_payload["timezone"])
        captured_at_ms = int(evidence_payload["captured_at_ms"])
        training_cutoff_ms = int(evidence_payload["training_cutoff_ms"])
        evidence_id = str(evidence_payload["evidence_id"])
        pv_slot_cap_kwh = float(evidence_payload["pv_slot_cap_kwh"])
        ev_active_threshold_kwh = float(evidence_payload["ev_active_threshold_kwh"])
    else:
        if not fallback_timezone:
            raise ValueError("legacy traces require --timezone")
        fidelity = "quantile_fallback"
        residuals = ()
        settings = ScenarioGenerationSettings()
        market = _market_from_problem(trace, bundle)
        timezone = fallback_timezone
        captured_at_ms = int(trace["generated_at_ms"])
        training_cutoff_ms = _training_cutoff(trace)
        evidence_id = f"legacy-replay:{captured_at_ms}:{training_cutoff_ms}"
        pv_slot_cap_kwh = (
            float(fallback_pv_slot_cap_kwh)
            if fallback_pv_slot_cap_kwh is not None
            else _infer_pv_slot_cap(trace)
        )
        ev_active_threshold_kwh = _ev_threshold(trace)
    eligible_history = tuple(
        item for item in history if item.slot.end_ms <= training_cutoff_ms
    )
    request = ScenarioBuildRequest(
        bundle,
        ScenarioEvidenceSnapshot(
            evidence_id,
            captured_at_ms,
            training_cutoff_ms,
            timezone,
            eligible_history,
            residuals,
            pv_slot_cap_kwh,
            ev_active_threshold_kwh,
        ),
        settings,
        market,
    )
    return fidelity, ScenarioBuilder().build(request)


def _bundle(trace: dict) -> tuple[ForecastDistributionBundle, bool]:
    load_payload = trace["load"]
    pv_payload = trace["pv"]
    ev_payload = trace["ev"]
    load_slots = tuple(_forecast_slot(row) for row in load_payload["slots"])
    components = tuple(
        LoadForecastComponent(
            str(item["component_key"]),
            str(item["model_version"]),
            int(item["training_cutoff_ms"]),
            tuple(_forecast_slot(row) for row in item["slots"]),
        )
        for item in load_payload.get("components", ())
    )
    components_complete = True
    try:
        load = LoadForecast(
            str(load_payload["forecast_id"]),
            int(load_payload["generated_at_ms"]),
            int(load_payload["training_cutoff_ms"]),
            str(load_payload["model_version"]),
            load_slots,
            components,
        )
    except ValueError as err:
        if "components must sum" not in str(err):
            raise
        load = LoadForecast(
            str(load_payload["forecast_id"]),
            int(load_payload["generated_at_ms"]),
            int(load_payload["training_cutoff_ms"]),
            str(load_payload["model_version"]),
            load_slots,
        )
        components_complete = False
    pv = PvForecast(
        str(pv_payload["forecast_id"]),
        int(pv_payload["generated_at_ms"]),
        int(pv_payload["training_cutoff_ms"]),
        str(pv_payload["model_version"]),
        tuple(_forecast_slot(row) for row in pv_payload["slots"]),
    )
    ev = EvForecast(
        str(ev_payload["forecast_id"]),
        int(ev_payload["generated_at_ms"]),
        int(ev_payload["training_cutoff_ms"]),
        str(ev_payload["model_version"]),
        tuple(_ev_slot(row) for row in ev_payload["slots"]),
    )
    return ForecastDistributionBundle(load, pv, ev), components_complete


def _forecast_slot(row) -> ForecastSlot:
    return ForecastSlot(
        SlotKey(int(row[0]), int(row[1])),
        _energy(row),
        _quality(row[6], row[7]),
    )


def _ev_slot(row) -> EvForecastSlot:
    return EvForecastSlot(
        SlotKey(int(row[0]), int(row[1])),
        _energy(row),
        float(row[6]),
        float(row[7]),
        _quality(row[8], row[9]),
    )


def _energy(row) -> QuantileEnergy:
    return QuantileEnergy(
        float(row[2]),
        None if row[3] is None else float(row[3]),
        None if row[4] is None else float(row[4]),
        int(row[5]),
    )


def _quality(coverage, flags) -> DataQuality:
    return DataQuality(
        float(coverage), tuple(QualityFlag(str(value)) for value in flags)
    )


def _market_from_replay(rows) -> tuple[ScenarioMarketSlot, ...]:
    return tuple(
        ScenarioMarketSlot(
            SlotKey(int(row[0]), int(row[1])),
            float(row[2]),
            float(row[3]),
            str(row[4]),
        )
        for row in rows
    )


def _market_from_problem(trace, bundle) -> tuple[ScenarioMarketSlot, ...]:
    rows = (trace.get("optimization_problem") or {}).get("market") or ()
    if len(rows) != len(bundle.load.slots):
        return ()
    return tuple(
        ScenarioMarketSlot(
            slot.slot,
            float(row[1]),
            float(row[2]),
            str(row[3]),
        )
        for slot, row in zip(bundle.load.slots, rows, strict=True)
    )


def _training_cutoff(trace) -> int:
    build = (trace.get("shadow_evaluation") or {}).get("scenario_build") or {}
    if build.get("training_cutoff_ms") is not None:
        return int(build["training_cutoff_ms"])
    return min(int(trace[name]["training_cutoff_ms"]) for name in ("load", "pv", "ev"))


def _ev_threshold(trace) -> float:
    policy = (trace.get("optimization_problem") or {}).get("ev_policy") or {}
    return float(policy.get("ev_active_threshold_w", 300.0)) / 1000.0 * SLOT_H


def _infer_pv_slot_cap(trace) -> float:
    values = [
        float(value)
        for row in trace["pv"]["slots"]
        for value in (row[2], row[4])
        if value is not None
    ]
    for path in (trace.get("scenarios") or {}).get("paths", ()):
        values.extend(float(row[2]) for row in path.get("slots", ()))
    return max(values, default=0.0)


def _serialized_result(trace, fidelity, result) -> dict[str, object]:
    scenarios = result.scenarios
    return {
        "generated_at_ms": int(trace["generated_at_ms"]),
        "replay_fidelity": fidelity,
        "status": result.status.value,
        "model_version": result.diagnostics.model_version,
        "input_fingerprint": result.diagnostics.input_fingerprint,
        "rejected_reasons": list(result.diagnostics.rejected_reasons),
        "scenarios": [
            {
                "id": path.scenario_id,
                "probability": path.probability,
                "slots": [
                    [
                        slot.slot.start_ms,
                        slot.house_load_kwh,
                        slot.pv_generation_kwh,
                        slot.ev_charge_kwh,
                        slot.import_price_ct_per_kwh,
                        slot.export_price_ct_per_kwh,
                        slot.price_is_firm,
                    ]
                    for slot in path.slots
                ],
            }
            for path in (scenarios.scenarios if scenarios is not None else ())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild scenarios from local copies of forecast traces and the "
            "feature store. Never run this historical replay on Home Assistant."
        )
    )
    parser.add_argument("--trace-dir", required=True, type=Path)
    parser.add_argument("--feature-store", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timezone")
    parser.add_argument("--pv-slot-cap-kwh", type=float)
    parser.add_argument("--start-ms", type=int, default=0)
    parser.add_argument("--end-ms", type=int, default=2**63 - 1)
    args = parser.parse_args()

    store = CompressedFeatureStore(args.feature_store)
    store.initialize()
    if store.last_error:
        raise SystemExit(f"feature store unavailable: {store.last_error}")
    history = store.load(0, 2**63 - 1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        for path in sorted(args.trace_dir.glob("*/*.json.gz")):
            trace = json.loads(gzip.decompress(path.read_bytes()))
            generated = int(trace.get("generated_at_ms", -1))
            if not args.start_ms <= generated <= args.end_ms:
                continue
            fidelity, result = rebuild_trace(
                trace,
                history,
                fallback_timezone=args.timezone,
                fallback_pv_slot_cap_kwh=args.pv_slot_cap_kwh,
            )
            output.write(
                json.dumps(
                    _serialized_result(trace, fidelity, result),
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )


if __name__ == "__main__":
    main()

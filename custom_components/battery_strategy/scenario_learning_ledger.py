"""Bounded observational ledger for future offline scenario learning."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import shutil
import uuid
from pathlib import Path

from .contracts import OptimizationProblem, OptimizationResult, ScenarioBuildResult

SCHEMA_VERSION = 2
RETENTION_DAYS = 400
MAX_BYTES = 128 * 1024 * 1024
HOUR_MS = 60 * 60 * 1000


def append_scenario_learning_vintage(
    root: Path,
    *,
    build_result: ScenarioBuildResult,
    optimization_problem: OptimizationProblem,
    optimization_result: OptimizationResult | None,
) -> Path | None:
    """Write one hourly vintage plus EV-state boundaries, never read by planning."""
    generated_at_ms = optimization_problem.as_of_ms
    hour_ms = generated_at_ms // HOUR_MS * HOUR_MS
    active = bool(
        optimization_problem.forecast.ev.slots
        and optimization_problem.forecast.ev.slots[0].active_probability >= 0.5
    )
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".ev_state"
    previous = marker.read_text(encoding="ascii").strip() if marker.exists() else ""
    event = previous not in ("", str(int(active)))
    marker_tmp = root / f".ev_state.{uuid.uuid4().hex}.tmp"
    marker_tmp.write_text(str(int(active)), encoding="ascii")
    os.replace(marker_tmp, marker)
    stamp = dt.datetime.fromtimestamp(hour_ms / 1000.0, dt.UTC)
    directory = root / stamp.strftime("%Y/%m/%d")
    suffix = f"-ev-{generated_at_ms}" if event else ""
    destination = directory / f"{stamp.strftime('%H00')}{suffix}.json.gz"
    if destination.exists():
        return None
    directory.mkdir(parents=True, exist_ok=True)
    payload = _payload(build_result, optimization_problem, optimization_result)
    temporary = directory / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
    os.replace(temporary, destination)
    _prune(root, generated_at_ms)
    return destination


def _payload(build_result, problem, result) -> dict[str, object]:
    diagnostics = build_result.diagnostics
    scenarios = build_result.scenarios
    return {
        "schema_version": SCHEMA_VERSION,
        "observational_only": True,
        "generated_at_ms": problem.as_of_ms,
        "training_cutoff_ms": diagnostics.training_cutoff_ms,
        "forecast": {
            "load_model": problem.forecast.load.model_version,
            "pv_model": problem.forecast.pv.model_version,
            "ev_model": problem.forecast.ev.model_version,
            "slots": [
                [
                    load.slot.start_ms,
                    load.energy.p10_kwh,
                    load.energy.p50_kwh,
                    load.energy.p90_kwh,
                    pv.energy.p10_kwh,
                    pv.energy.p50_kwh,
                    pv.energy.p90_kwh,
                    ev.energy.p10_kwh,
                    ev.energy.p50_kwh,
                    ev.energy.p90_kwh,
                    ev.active_probability,
                ]
                for load, pv, ev in zip(
                    problem.forecast.load.slots,
                    problem.forecast.pv.slots,
                    problem.forecast.ev.slots,
                    strict=True,
                )
            ],
        },
        "scenario_build": {
            "status": build_result.status.value,
            "model_version": diagnostics.model_version,
            "settings_seed": diagnostics.seed,
            "input_fingerprint": diagnostics.input_fingerprint,
            "rejected_reasons": list(diagnostics.rejected_reasons),
            "candidates": [
                [
                    item.weeks_ago,
                    item.component_distance,
                    item.repaired_slots,
                    item.excluded_component_values,
                    item.raw_weight,
                    item.probability,
                    item.selected,
                ]
                for item in diagnostics.candidates
            ],
        },
        "scenarios": (
            [
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
                for path in scenarios.scenarios
            ]
            if scenarios is not None
            else []
        ),
        "optimization_status": "completed" if result is not None else "failed",
        "decision": (
            {
                "slot_start_ms": result.decision.slot.slot.start_ms,
                "required_charge_kwh": result.decision.slot.required_charge_kwh,
                "discharge_budget_kwh": result.decision.slot.discharge_budget_kwh,
                "mode": result.decision.slot.mode.value,
            }
            if result is not None and result.decision is not None
            else None
        ),
        "projection": (
            [
                [
                    slot.slot.start_ms,
                    slot.planned_charge_kwh,
                    slot.planned_discharge_kwh,
                    slot.expected_soc_end_pct,
                ]
                for slot in result.projection.slots
            ]
            if result is not None
            else []
        ),
    }


def _prune(root: Path, generated_at_ms: int) -> None:
    cutoff = generated_at_ms - RETENTION_DAYS * 24 * HOUR_MS
    files = sorted(root.glob("*/*/*/*.json.gz"), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in files)
    for path in files:
        timestamp_ms = int(path.stat().st_mtime * 1000)
        if timestamp_ms >= cutoff and total <= MAX_BYTES:
            continue
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size
    for directory in sorted(root.glob("*/*/*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            shutil.rmtree(directory, ignore_errors=True)

"""Non-authoritative parity evaluation for the Phase-5 optimizer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .contracts import (
    BatteryConstraints,
    BatteryState,
    CommercialPolicy,
    ForecastBundle,
    MarketSlot,
    OptimizationProblem,
)
from .economic_optimizer import DynamicProgrammingOptimizer

SLOT_H = 0.25
RETENTION_S = 14 * 24 * 60 * 60
MAX_RECORDS = 1500

# Exact deltas remain visible, while the release gate accounts for the legacy
# plan's serialized precision and the 5 W live-command resolution.
EXACT_ACTION_TOLERANCE_KWH = 0.00005
EXACT_BUDGET_TOLERANCE_KWH = 0.0005
EXACT_SOC_TOLERANCE_PCT = 0.011
LIVE_POWER_RESOLUTION_W = 5.0
OPERATIONAL_ACTION_TOLERANCE_KWH = LIVE_POWER_RESOLUTION_W * SLOT_H / 1000.0
OPERATIONAL_BUDGET_TOLERANCE_KWH = (
    OPERATIONAL_ACTION_TOLERANCE_KWH + 0.001
)
LEGACY_SOC_ROUNDING_PCT = 0.01


def evaluate_optimizer_shadow(
    *,
    intervals,
    forecast: ForecastBundle,
    start_energy_kwh: float,
    legacy_plan: dict,
    constraints: BatteryConstraints,
    policy: CommercialPolicy,
    evaluated_at_ms: int,
) -> dict:
    """Compare one pure result with the authoritative legacy plan."""
    problem = OptimizationProblem(
        problem_id=(
            f"shadow:{evaluated_at_ms}:{forecast.load.forecast_id}:"
            f"{forecast.pv.forecast_id}"
        ),
        as_of_ms=max(
            evaluated_at_ms,
            forecast.load.generated_at_ms,
            forecast.pv.generated_at_ms,
        ),
        forecast=forecast,
        market=tuple(
            MarketSlot(
                load_slot.slot,
                float(interval["price_eur"]) * 100.0,
                policy.export_opportunity_ct_per_kwh,
                "phase5_shadow_snapshot",
            )
            for interval, load_slot in zip(
                intervals, forecast.load.slots, strict=True
            )
        ),
        battery=BatteryState(
            evaluated_at_ms,
            max(
                constraints.min_soc_pct,
                min(
                    constraints.max_soc_pct,
                    100.0 * start_energy_kwh / constraints.capacity_kwh,
                ),
            ),
        ),
        constraints=constraints,
        policy=policy,
    )
    candidate = DynamicProgrammingOptimizer().optimize(problem)
    legacy_points = tuple(legacy_plan.get("points") or ())
    if len(legacy_points) != len(candidate.slots):
        raise ValueError("legacy and pure plans have different slot counts")

    charge_deltas = []
    discharge_deltas = []
    budget_deltas = []
    soc_deltas = []
    exact_mismatch_slots = 0
    operational_mismatch_slots = 0
    operational_soc_tolerance_pct = LEGACY_SOC_ROUNDING_PCT + (
        100.0 * OPERATIONAL_ACTION_TOLERANCE_KWH / constraints.capacity_kwh
    )
    for legacy, pure in zip(legacy_points, candidate.slots, strict=True):
        charge_delta = abs(
            float(legacy.get("charge_fc_w", 0.0)) * SLOT_H / 1000.0
            - pure.planned_charge_kwh
        )
        discharge_delta = abs(
            float(legacy.get("discharge_fc_w", 0.0)) * SLOT_H / 1000.0
            - pure.planned_discharge_kwh
        )
        budget_delta = abs(
            float(legacy.get("discharge_budget_kwh", 0.0))
            - pure.discharge_budget_kwh
        )
        soc_delta = abs(
            float(legacy.get("soc_pct", 0.0)) - pure.expected_soc_start_pct
        )
        charge_deltas.append(charge_delta)
        discharge_deltas.append(discharge_delta)
        budget_deltas.append(budget_delta)
        soc_deltas.append(soc_delta)
        if (
            charge_delta > EXACT_ACTION_TOLERANCE_KWH
            or discharge_delta > EXACT_ACTION_TOLERANCE_KWH
            or budget_delta > EXACT_BUDGET_TOLERANCE_KWH
            or soc_delta > EXACT_SOC_TOLERANCE_PCT
        ):
            exact_mismatch_slots += 1
        if (
            charge_delta > OPERATIONAL_ACTION_TOLERANCE_KWH
            or discharge_delta > OPERATIONAL_ACTION_TOLERANCE_KWH
            or budget_delta > OPERATIONAL_BUDGET_TOLERANCE_KWH
            or soc_delta > operational_soc_tolerance_pct
        ):
            operational_mismatch_slots += 1

    legacy_daily = legacy_plan.get("daily_costs") or {}
    legacy_baseline = sum(
        float(value.get("base_eur", 0.0)) for value in legacy_daily.values()
    )
    legacy_optimized = sum(
        float(value.get("with_bat_eur", 0.0)) for value in legacy_daily.values()
    )
    return {
        "ts_ms": int(evaluated_at_ms),
        "problem_id": problem.problem_id,
        "optimizer_version": candidate.optimizer_version,
        "status": "match" if operational_mismatch_slots == 0 else "mismatch",
        "exact_status": "match" if exact_mismatch_slots == 0 else "mismatch",
        "slot_count": len(candidate.slots),
        "mismatch_slots": operational_mismatch_slots,
        "exact_mismatch_slots": exact_mismatch_slots,
        "max_charge_delta_kwh": round(max(charge_deltas, default=0.0), 6),
        "max_discharge_delta_kwh": round(max(discharge_deltas, default=0.0), 6),
        "max_budget_delta_kwh": round(max(budget_deltas, default=0.0), 6),
        "max_soc_delta_pct": round(max(soc_deltas, default=0.0), 4),
        "operational_action_tolerance_kwh": OPERATIONAL_ACTION_TOLERANCE_KWH,
        "operational_budget_tolerance_kwh": OPERATIONAL_BUDGET_TOLERANCE_KWH,
        "operational_soc_tolerance_pct": round(
            operational_soc_tolerance_pct, 4
        ),
        "baseline_cost_delta_eur": round(
            candidate.baseline_cost_eur - legacy_baseline, 6
        ),
        "optimized_cost_delta_eur": round(
            candidate.optimized_cost_eur - legacy_optimized, 6
        ),
    }


def safe_evaluate_optimizer_shadow(**kwargs) -> dict:
    """Return an error record instead of affecting authoritative planning."""
    try:
        return evaluate_optimizer_shadow(**kwargs)
    except Exception as exc:  # noqa: BLE001 - shadow cannot fail production
        return {
            "ts_ms": int(kwargs.get("evaluated_at_ms", time.time() * 1000)),
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def append_shadow_record(path: str | Path, record: dict, *, now_ms: int) -> None:
    """Persist a bounded compact JSONL trace outside Home Assistant Recorder."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if int(item.get("ts_ms", 0)) >= now_ms - RETENTION_S * 1000:
                records.append(_classify_retained_record(item))
    records.append(_classify_retained_record(dict(record)))
    records = records[-MAX_RECORDS:]
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _classify_retained_record(record: dict) -> dict:
    """Add the operational verdict to retained pre-cutover summaries."""
    item = dict(record)
    if item.get("status") not in {"match", "mismatch"}:
        return item
    item.setdefault("exact_status", item.get("status"))
    item.setdefault("exact_mismatch_slots", int(item.get("mismatch_slots", 0) or 0))
    operational_match = (
        float(item.get("max_charge_delta_kwh", 0.0) or 0.0)
        <= OPERATIONAL_ACTION_TOLERANCE_KWH
        and float(item.get("max_discharge_delta_kwh", 0.0) or 0.0)
        <= OPERATIONAL_ACTION_TOLERANCE_KWH
        and float(item.get("max_budget_delta_kwh", 0.0) or 0.0)
        <= OPERATIONAL_BUDGET_TOLERANCE_KWH
        and float(item.get("max_soc_delta_pct", 0.0) or 0.0) <= 0.035
    )
    item["status"] = "match" if operational_match else "mismatch"
    item["mismatch_slots"] = 0 if operational_match else int(
        item.get("exact_mismatch_slots", 0) or 0
    )
    item.setdefault(
        "operational_action_tolerance_kwh", OPERATIONAL_ACTION_TOLERANCE_KWH
    )
    item.setdefault(
        "operational_budget_tolerance_kwh", OPERATIONAL_BUDGET_TOLERANCE_KWH
    )
    return item

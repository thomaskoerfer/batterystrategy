"""Bounded evaluation helpers for the temporary compiler shadow."""

from __future__ import annotations

import json
from pathlib import Path

from .const import DISCHARGE_LOAD
from .plan_models import PlanLiveDirective

ENERGY_TOLERANCE_KWH = 0.001


def compare_published_directives(
    authoritative: PlanLiveDirective,
    candidate: PlanLiveDirective,
    *,
    discharge_mode: str,
    captured_at_ms: int,
) -> dict:
    """Compare semantics relevant to live control, not object identity."""
    mismatches = []
    exact_fields = (
        "slot_start_ts",
        "slot_end_ts",
        "pv_charge_allowed",
        "grid_charge_allowed",
        "must_charge_w",
        "battery_min_soc_pct",
        "battery_max_soc_pct",
    )
    for field in exact_fields:
        if getattr(authoritative, field) != getattr(candidate, field):
            mismatches.append(field)
    required_delta = abs(
        authoritative.must_charge_remaining_kwh - candidate.must_charge_remaining_kwh
    )
    if required_delta > ENERGY_TOLERANCE_KWH:
        mismatches.append("must_charge_remaining_kwh")
    budget_delta = abs(
        authoritative.discharge_budget_kwh - candidate.discharge_budget_kwh
    )
    # Load-following does not consume commercial budget; both directives merely
    # carry an enable signal for the established live-controller adapter.
    if discharge_mode != DISCHARGE_LOAD and budget_delta > ENERGY_TOLERANCE_KWH:
        mismatches.append("discharge_budget_kwh")
    return {
        "captured_at_ms": int(captured_at_ms),
        "slot_start_ms": int(authoritative.slot_start_ts),
        "status": "match" if not mismatches else "mismatch",
        "mismatch_fields": mismatches,
        "required_charge_delta_kwh": round(required_delta, 6),
        "discharge_budget_delta_kwh": round(budget_delta, 6),
    }


def append_bounded_record(
    path: Path,
    record: dict,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    retain_lines: int = 4000,
) -> None:
    """Append one compact record and bound the temporary trace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    if path.stat().st_size <= max_bytes:
        return
    lines = path.read_text(encoding="utf-8").splitlines()[-retain_lines:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

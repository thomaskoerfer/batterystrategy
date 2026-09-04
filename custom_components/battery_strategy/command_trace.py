"""Bounded diagnostic command-trace persistence."""

from __future__ import annotations

import datetime as dt
import json
from collections import deque
from dataclasses import asdict
from pathlib import Path

COMMAND_TRACE_FILE = "battery_strategy_command_trace.jsonl"
COMMAND_TRACE_MAX_BYTES = 64 * 1024 * 1024
COMMAND_TRACE_RETAIN_LINES = 50000


def append_command_trace(path: Path, data: dict) -> None:
    """Append one compact observation without influencing live control."""
    now = dt.datetime.now(dt.timezone.utc)
    command = data["command"]
    calculated_command = data["calculated_command"]
    plan = data["plan"]
    inputs = data["inputs"]
    diagnostics = data["live_diagnostics"]
    item = {
        "ts": now.timestamp(),
        "iso": now.isoformat(),
        "mode": command.mode.value,
        "power_w": command.power_w,
        "reason": command.reason,
        "calculated_mode": calculated_command.mode.value,
        "calculated_power_w": calculated_command.power_w,
        "calculated_reason": calculated_command.reason,
        "send_commands": data["send_commands"],
        "strategy_enabled": data["strategy_enabled"],
        "grid_import_w": round(inputs.grid_import_w),
        "grid_export_w": round(inputs.grid_export_w),
        "pv_w": round(inputs.pv_generation_w),
        "battery_power_w": round(inputs.battery_discharge_w - inputs.battery_charge_w),
        "ev_power_w": round(inputs.ev_charge_w),
        "soc_pct": round(inputs.soc_pct, 1),
        "soc_control_ready": data.get("soc_control_ready"),
        "soc_estimate_stale": data.get("soc_estimate_stale"),
        "current_plan_points": len(plan.points),
        "optimizer_age_s": data.get("optimizer_age_s"),
        "plan_mode": plan.current_mode,
        "plan_power_w": plan.current_power_w,
        "plan_to_live": asdict(data["plan_to_live"]),
        "live_diagnostics": asdict(diagnostics),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, separators=(",", ":")) + "\n")
    if path.stat().st_size > COMMAND_TRACE_MAX_BYTES:
        _trim_command_trace(path)


def _trim_command_trace(path: Path) -> None:
    """Bound trace disk usage without rewriting it during normal updates."""
    with path.open("r", encoding="utf-8") as handle:
        retained = deque(handle, maxlen=COMMAND_TRACE_RETAIN_LINES)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.writelines(retained)
    temporary.replace(path)

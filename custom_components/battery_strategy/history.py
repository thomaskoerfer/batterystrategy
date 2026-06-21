"""Persistence helpers for Battery Strategy HACS state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


DEFAULT_STATE = {
    "samples": [],
    "pv_bias": 1.0,
    "load_bias": 1.0,
    "actual_daily_savings": {},
    "eex_cache": {},
}


def load_optimizer_state(path: Path) -> dict:
    """Load optimizer state with defaults."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    merged = dict(DEFAULT_STATE)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def save_optimizer_state(path: Path, data: dict) -> None:
    """Persist optimizer state."""
    write_json_atomic(path, data)


def write_json_atomic(path: Path, data: object) -> None:
    """Write JSON by replacing the target only after the new file is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def append_sample(state: dict, sample: dict, max_samples: int = 12000) -> dict:
    """Append a live sample and trim history."""
    samples = state.setdefault("samples", [])
    if isinstance(samples, list):
        samples.append(sample)
        state["samples"] = samples[-max_samples:]
    else:
        state["samples"] = [sample]
    return state

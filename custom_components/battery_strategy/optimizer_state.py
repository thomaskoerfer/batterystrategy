"""Atomic compressed persistence for optimizer runtime state."""

from __future__ import annotations

import gzip
import json
import os
import uuid
from pathlib import Path
from typing import Any


def load_state_document(path: str | Path) -> dict[str, Any] | None:
    """Load JSON state, accepting both gzip and legacy plain JSON."""
    state_path = Path(path)
    try:
        raw = state_path.read_bytes()
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_state_document(path: str | Path, data: dict[str, Any]) -> None:
    """Atomically save optimizer state as compressed JSON."""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(
        f"{state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    with gzip.open(tmp, "wb", compresslevel=6) as handle:
        handle.write(payload)
    os.replace(tmp, state_path)


def last_known_soc_pct(path: str | Path) -> float | None:
    """Return the newest persisted valid battery SoC."""
    data = load_state_document(path)
    if data is None:
        return None
    candidates = [data.get("last_known_soc_pct")]
    for sample in reversed(data.get("samples") or []):
        if isinstance(sample, dict):
            candidates.append(sample.get("soc"))
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 100.0:
            return value
    return None


def last_optimizer_output(path: str | Path) -> dict[str, Any] | None:
    """Return the last persisted optimizer output for startup hydration."""
    data = load_state_document(path)
    if data is None:
        return None
    output = data.get("last_output")
    return dict(output) if isinstance(output, dict) and output else None


def runtime_snapshot(path: str | Path) -> tuple[float | None, dict[str, Any] | None]:
    """Load startup values in one executor-backed disk read."""
    data = load_state_document(path)
    if data is None:
        return None, None
    output = data.get("last_output")
    output = dict(output) if isinstance(output, dict) and output else None
    candidates = [data.get("last_known_soc_pct")]
    candidates.extend(
        sample.get("soc")
        for sample in reversed(data.get("samples") or [])
        if isinstance(sample, dict)
    )
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 100.0:
            return value, output
    return None, output

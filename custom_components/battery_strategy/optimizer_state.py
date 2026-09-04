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

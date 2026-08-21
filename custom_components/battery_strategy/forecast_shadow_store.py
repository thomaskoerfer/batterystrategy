"""Bounded persistence for recorder-independent forecast comparisons."""

from __future__ import annotations

import gzip
import json
import os
import uuid
from pathlib import Path

from .contracts import HistoricalFeatureSlot, QualityFlag
from .contracts.common import SLOT_MS

SHADOW_TRACE_SCHEMA_VERSION = 1
SHADOW_TRACE_RETENTION_DAYS = 14


class ForecastShadowTraceStore:
    """Keep one compact comparison per generation slot outside HA Recorder."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: dict[int, dict[str, object]] = {}
        self.last_error: str | None = None
        self._initialized = False

    def initialize(self) -> None:
        """Load an existing trace, isolating malformed persistence."""
        try:
            self._entries = {
                int(item["generation_slot_start_ms"]): item for item in self._read()
            }
            self.last_error = None
        except (
            OSError,
            EOFError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as err:
            self._entries = {}
            self.last_error = f"{type(err).__name__}: {err}"
        self._initialized = True

    def record(
        self,
        comparison: dict[str, object],
        history: tuple[HistoricalFeatureSlot, ...],
    ) -> dict[str, object]:
        """Persist one comparison and mature pending next-slot evaluations."""
        if not self._initialized:
            self.initialize()
        generated_at_ms = int(comparison["generated_at_ms"])
        generation_slot = generated_at_ms // SLOT_MS * SLOT_MS
        changed = False
        if generation_slot not in self._entries:
            item = dict(comparison)
            item["generation_slot_start_ms"] = generation_slot
            self._entries[generation_slot] = item
            changed = True
        changed = self._mature(history) or changed
        newest = max(self._entries, default=generation_slot)
        cutoff = newest - SHADOW_TRACE_RETENTION_DAYS * 86_400_000
        retained = {key: value for key, value in self._entries.items() if key >= cutoff}
        if len(retained) != len(self._entries):
            self._entries = retained
            changed = True
        if changed:
            self._write()
        self.last_error = None
        return self.diagnostics()

    def diagnostics(self) -> dict[str, object]:
        """Return bounded parity and matured forecast-accuracy diagnostics."""
        ordered = [self._entries[key] for key in sorted(self._entries)]
        latest = ordered[-1] if ordered else {}
        matured = [
            item
            for item in ordered
            if "actual_load_kwh" in item or "actual_pv_kwh" in item
        ]
        return {
            "authoritative": False,
            "status": latest.get("status", "not_started"),
            "reason": latest.get("reason"),
            "trace_count": len(ordered),
            "matured_count": len(matured),
            "history_slot_count": latest.get("history_slot_count", 0),
            "load_usable_slots": latest.get("load_usable_slots", 0),
            "pv_usable_slots": latest.get("pv_usable_slots", 0),
            "history_span_days": latest.get("history_span_days", 0.0),
            "load_parity_mae_w": latest.get("load_mae_delta_w"),
            "pv_parity_mae_w": latest.get("pv_mae_delta_w"),
            "production_load_mae_kwh": _mae(matured, "production_load_error_kwh"),
            "shadow_load_mae_kwh": _mae(matured, "shadow_load_error_kwh"),
            "production_pv_mae_kwh": _mae(matured, "production_pv_error_kwh"),
            "shadow_pv_mae_kwh": _mae(matured, "shadow_pv_error_kwh"),
            "file_size_bytes": self._file_size(),
            "last_error": self.last_error,
        }

    def _mature(self, history: tuple[HistoricalFeatureSlot, ...]) -> bool:
        actual_by_start = {item.slot.start_ms: item for item in history}
        changed = False
        for item in self._entries.values():
            if item.get("actual_evaluation_complete"):
                continue
            target = item.get("evaluation_slot_start_ms")
            actual = actual_by_start.get(int(target)) if target is not None else None
            if actual is None or actual.quality.coverage < 0.999:
                continue
            flags = set(actual.quality.flags)
            if QualityFlag.RESTART_GAP in flags:
                continue
            load_valid = not flags & {
                QualityFlag.MISSING_GRID,
                QualityFlag.MISSING_PV,
                QualityFlag.MISSING_BATTERY,
                QualityFlag.MISSING_EV,
            }
            if load_valid:
                actual_load = actual.house_load_no_ev_kwh
                item["actual_load_kwh"] = actual_load
                item["production_load_error_kwh"] = abs(
                    float(item["production_load_kwh"]) - actual_load
                )
                item["shadow_load_error_kwh"] = abs(
                    float(item["shadow_load_kwh"]) - actual_load
                )
                changed = True
            pv_valid = QualityFlag.MISSING_PV not in flags
            if pv_valid:
                actual_pv = actual.pv_generation_kwh
                item["actual_pv_kwh"] = actual_pv
                item["production_pv_error_kwh"] = abs(
                    float(item["production_pv_kwh"]) - actual_pv
                )
                item["shadow_pv_error_kwh"] = abs(
                    float(item["shadow_pv_kwh"]) - actual_pv
                )
                changed = True
            item["actual_evaluation_complete"] = True
            item["actual_load_valid"] = load_valid
            item["actual_pv_valid"] = pv_valid
            changed = True
        return changed

    def _read(self) -> tuple[dict[str, object], ...]:
        if not self.path.exists():
            return ()
        envelope = json.loads(gzip.decompress(self.path.read_bytes()))
        if envelope.get("schema_version") != SHADOW_TRACE_SCHEMA_VERSION:
            raise ValueError("unsupported forecast shadow trace schema")
        entries = envelope.get("entries", [])
        if not isinstance(entries, list):
            raise TypeError("forecast shadow trace entries must be a list")
        return tuple(dict(item) for item in entries)

    def _write(self) -> None:
        envelope = {
            "schema_version": SHADOW_TRACE_SCHEMA_VERSION,
            "retention_days": SHADOW_TRACE_RETENTION_DAYS,
            "entries": [self._entries[key] for key in sorted(self._entries)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        with gzip.open(tmp, "wb", compresslevel=6) as handle:
            handle.write(json.dumps(envelope, separators=(",", ":")).encode())
        os.replace(tmp, self.path)

    def _file_size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def _mae(items: list[dict[str, object]], key: str) -> float | None:
    values = [float(item[key]) for item in items if item.get(key) is not None]
    return round(sum(values) / len(values), 5) if values else None

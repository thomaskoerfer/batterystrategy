"""Typed ownership and persistence for planning application state."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .optimizer_state import load_state_document, save_state_document
from .runtime_measurements import migrate_state_sample_v9, normalize_samples

SLOTS_PER_DAY = 96
TRACE_MIN_INTERVAL_S = 240
TRACE_RETENTION_DAYS = 14
TRACE_MAX_POINTS = 8000
STATE_SCHEMA_VERSION = 11
PLANNING_STATE_FILENAME = "battery_strategy_optimizer_state.json"
_STATE_LOCK = threading.Lock()
_ACTIVE_LEASES: dict[str, str] = {}


class StalePlanningStateLease(RuntimeError):
    """Raised when an obsolete runtime generation attempts a state write."""


@dataclass(slots=True)
class ForecastLearningState:
    """State owned by forecast calibration and evaluation."""

    samples: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    backtests: list[dict[str, Any]] = field(default_factory=list)
    pv_bias: float = 1.0
    load_bias: float = 1.0
    pv_bias_slots: list[float] = field(default_factory=lambda: [1.0] * SLOTS_PER_DAY)
    load_bias_slots: list[float] = field(default_factory=lambda: [1.0] * SLOTS_PER_DAY)


@dataclass(slots=True)
class SimulationState:
    """State owned by the display-only virtual battery simulation."""

    energy_kwh: float
    last_ts: float | None = None
    last_mode: str = "idle"
    last_power_w: float = 0.0
    trace: list[dict[str, Any]] = field(default_factory=list)
    last_known_soc_pct: float | None = None


@dataclass(slots=True)
class MarketState:
    """State owned by market enrichment."""

    eex_cache: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SavingsState:
    """State owned by estimated and measured savings accounting."""

    estimated_daily: dict[str, float] = field(default_factory=dict)
    actual_daily: dict[str, dict[str, float]] = field(default_factory=dict)
    tracker: dict[str, Any] = field(default_factory=dict)
    archived_eur: float = 0.0
    tracker_was_persisted: bool = False
    archived_was_persisted: bool = False


@dataclass(slots=True)
class PublicationState:
    """State owned by planning-result persistence and display restore."""

    last_output: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlanningOwnerState:
    """Typed aggregate whose sections retain their domain ownership."""

    captured_at_ms: int
    forecast: ForecastLearningState
    simulation: SimulationState
    market: MarketState
    savings: SavingsState
    publication: PublicationState
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanningStateStore:
    """Sole load, migration, validation, serialization and write owner."""

    path: str
    lease_token: str | None = None

    @classmethod
    def claim(cls, path: str | Path) -> PlanningStateStore:
        """Claim the current in-process writer generation for a state path."""
        normalized = str(Path(path))
        token = uuid.uuid4().hex
        with _STATE_LOCK:
            _ACTIVE_LEASES[normalized] = token
        return cls(normalized, token)

    def load(self, settings, captured_at_ms: int) -> PlanningOwnerState:
        """Load and type the unchanged atomic schema-11 document."""
        document = _load_document(settings, self.path)
        try:
            return _owner_state_from_document(document, captured_at_ms)
        except (KeyError, TypeError, ValueError):
            return _owner_state_from_document(
                _default_document(settings), captured_at_ms
            )

    def revoke(self) -> None:
        """Prevent this lifecycle generation from making later writes."""
        if self.lease_token is None:
            return
        with _STATE_LOCK:
            if _ACTIVE_LEASES.get(self.path) == self.lease_token:
                _ACTIVE_LEASES.pop(self.path, None)

    def save(self, state: PlanningOwnerState) -> bool:
        """Atomically write unless this run or lifecycle generation is stale."""
        with _STATE_LOCK:
            if self.lease_token is not None:
                if _ACTIVE_LEASES.get(self.path) != self.lease_token:
                    raise StalePlanningStateLease(
                        "obsolete planning runtime cannot overwrite current state"
                    )
            existing = load_state_document(self.path)
            if (
                existing is not None
                and _output_timestamp_ms(existing.get("last_output"))
                > state.captured_at_ms
            ):
                return False
            save_state_document(self.path, self.to_document(state))
        return True

    def to_document(self, state: PlanningOwnerState) -> dict[str, Any]:
        """Serialize the exact existing schema-11 keys and meanings."""
        document = {
            **state.extra,
            "samples": state.forecast.samples,
            "predictions": state.forecast.predictions,
            "backtests": state.forecast.backtests,
            "pv_bias": state.forecast.pv_bias,
            "load_bias": state.forecast.load_bias,
            "pv_bias_slots": state.forecast.pv_bias_slots,
            "load_bias_slots": state.forecast.load_bias_slots,
            "virtual_energy_kwh": state.simulation.energy_kwh,
            "virtual_last_ts": state.simulation.last_ts,
            "virtual_last_mode": state.simulation.last_mode,
            "virtual_last_power_w": state.simulation.last_power_w,
            "virtual_trace": state.simulation.trace,
            "last_known_soc_pct": state.simulation.last_known_soc_pct,
            "eex_cache": state.market.eex_cache,
            "daily_savings": state.savings.estimated_daily,
            "actual_daily_savings": state.savings.actual_daily,
            "last_output": state.publication.last_output,
            "state_schema": STATE_SCHEMA_VERSION,
        }
        if state.savings.tracker_was_persisted or state.savings.tracker:
            document["savings_tracker"] = state.savings.tracker
        if state.savings.archived_was_persisted or state.savings.archived_eur != 0.0:
            document["actual_savings_archived_eur"] = state.savings.archived_eur
        return document

    def runtime_snapshot(self) -> tuple[float | None, dict[str, Any] | None]:
        """Read startup SoC and display output through this storage owner."""
        document = load_state_document(self.path)
        if document is None:
            return None, None
        output = document.get("last_output")
        display = dict(output) if isinstance(output, dict) and output else None
        candidates = [document.get("last_known_soc_pct")]
        candidates.extend(
            sample.get("soc")
            for sample in reversed(document.get("samples") or [])
            if isinstance(sample, dict)
        )
        for candidate in candidates:
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if 0.0 <= value <= 100.0:
                return value, display
        return None, display


def _clamp(value, lower, upper):
    return max(lower, min(upper, value))


def compact_virtual_trace(trace):
    if not isinstance(trace, list):
        return []
    out = []
    min_delta_ms = TRACE_MIN_INTERVAL_S * 1000
    for item in sorted(trace, key=lambda item: item.get("ts_ms", 0)):
        ts_ms = int(item.get("ts_ms", 0))
        if out and ts_ms - int(out[-1].get("ts_ms", 0)) < min_delta_ms:
            out[-1] = item
        else:
            out.append(item)
    return out


def normalize_slot_biases(arr, lo, hi):
    if not isinstance(arr, list) or len(arr) != SLOTS_PER_DAY:
        return [1.0] * SLOTS_PER_DAY
    out = []
    for value in arr:
        try:
            out.append(_clamp(float(value), lo, hi))
        except (TypeError, ValueError):
            out.append(1.0)
    return out


def _default_document(settings) -> dict[str, Any]:
    """Return one safe empty schema-11 document for the configured battery."""
    return {
        "samples": [],
        "predictions": [],
        "backtests": [],
        "pv_bias": 1.0,
        "load_bias": 1.0,
        "pv_bias_slots": [1.0] * SLOTS_PER_DAY,
        "load_bias_slots": [1.0] * SLOTS_PER_DAY,
        "virtual_energy_kwh": settings.battery_capacity_kwh * 0.5,
        "virtual_last_ts": None,
        "virtual_last_mode": "idle",
        "virtual_last_power_w": 0.0,
        "virtual_trace": [],
        "last_known_soc_pct": None,
        "eex_cache": {},
        "daily_savings": {},
        "actual_daily_savings": {},
        "last_output": {},
        "state_schema": STATE_SCHEMA_VERSION,
    }


def _load_document(settings, path: str) -> dict[str, Any]:
    default_state = _default_document(settings)
    try:
        data = load_state_document(path)
        if data is None:
            return default_state
        for key, value in default_state.items():
            data.setdefault(key, value)
        data.pop("forecast_shadow_trace", None)
        data.pop("forecast_parity_trace", None)
        if int(data.get("state_schema", 0)) < 4:
            data["virtual_energy_kwh"] = settings.battery_capacity_kwh * 0.5
            data["virtual_last_ts"] = None
            data["virtual_last_mode"] = "idle"
            data["virtual_last_power_w"] = 0.0
            data["virtual_trace"] = []
        if int(data.get("state_schema", 0)) < 9:
            data["samples"] = [
                migrate_state_sample_v9(sample)
                for sample in data.get("samples", [])
                if isinstance(sample, dict)
            ]
        else:
            data["samples"] = normalize_samples(data.get("samples", []))
        data["state_schema"] = STATE_SCHEMA_VERSION
        data["virtual_trace"] = compact_virtual_trace(data.get("virtual_trace", []))
        return data
    except (OSError, TypeError, ValueError):
        return default_state


def _owner_state_from_document(
    document: dict[str, Any], captured_at_ms: int
) -> PlanningOwnerState:
    known = {
        "state_schema",
        "samples",
        "predictions",
        "backtests",
        "pv_bias",
        "load_bias",
        "pv_bias_slots",
        "load_bias_slots",
        "virtual_energy_kwh",
        "virtual_last_ts",
        "virtual_last_mode",
        "virtual_last_power_w",
        "virtual_trace",
        "last_known_soc_pct",
        "eex_cache",
        "daily_savings",
        "actual_daily_savings",
        "savings_tracker",
        "actual_savings_archived_eur",
        "last_output",
    }
    return PlanningOwnerState(
        captured_at_ms=captured_at_ms,
        forecast=ForecastLearningState(
            samples=list(document["samples"]),
            predictions=list(document["predictions"]),
            backtests=list(document["backtests"]),
            pv_bias=float(document["pv_bias"]),
            load_bias=float(document["load_bias"]),
            pv_bias_slots=list(document["pv_bias_slots"]),
            load_bias_slots=list(document["load_bias_slots"]),
        ),
        simulation=SimulationState(
            energy_kwh=float(document["virtual_energy_kwh"]),
            last_ts=document["virtual_last_ts"],
            last_mode=str(document["virtual_last_mode"]),
            last_power_w=float(document["virtual_last_power_w"]),
            trace=list(document["virtual_trace"]),
            last_known_soc_pct=document["last_known_soc_pct"],
        ),
        market=MarketState(dict(document["eex_cache"])),
        savings=SavingsState(
            estimated_daily=dict(document["daily_savings"]),
            actual_daily=dict(document["actual_daily_savings"]),
            tracker=dict(document.get("savings_tracker") or {}),
            archived_eur=float(document.get("actual_savings_archived_eur", 0.0)),
            tracker_was_persisted="savings_tracker" in document,
            archived_was_persisted="actual_savings_archived_eur" in document,
        ),
        publication=PublicationState(dict(document["last_output"])),
        extra={key: value for key, value in document.items() if key not in known},
    )


def fallback_output(mode, reason, state: PublicationState, now_iso):
    out = dict(state.last_output)
    out["mode"] = mode
    out["reason"] = reason
    out["timestamp"] = now_iso
    return out


def _output_timestamp_ms(output: object) -> int:
    if not isinstance(output, dict):
        return -1
    try:
        return int(
            dt.datetime.fromisoformat(str(output.get("timestamp"))).timestamp() * 1000
        )
    except (TypeError, ValueError):
        return -1


def advance_virtual_energy(settings, state: SimulationState, now_ts):
    energy = _clamp(
        float(state.energy_kwh), settings.min_energy_kwh, settings.max_energy_kwh
    )
    if not state.last_ts:
        state.energy_kwh = energy
        return energy
    elapsed_h = max(0.0, (now_ts - float(state.last_ts)) / 3600.0)
    last_power_w = max(0.0, min(settings.max_power_w, float(state.last_power_w)))
    e_cmd = (last_power_w / 1000.0) * elapsed_h
    if state.last_mode in ("charge_grid", "charge_pv_surplus"):
        energy += e_cmd * settings.charge_efficiency
    elif state.last_mode.startswith("discharge_"):
        energy -= e_cmd / settings.discharge_efficiency
    energy = _clamp(energy, settings.min_energy_kwh, settings.max_energy_kwh)
    state.energy_kwh = energy
    return energy


def append_virtual_trace(
    state: SimulationState, ts_ms, date_str, soc_pct, mode, power_w
):
    if mode in ("charge_grid", "charge_pv_surplus"):
        charge_w = max(0.0, power_w)
        discharge_w = 0.0
    elif mode.startswith("discharge_"):
        charge_w = 0.0
        discharge_w = max(0.0, power_w)
    else:
        charge_w = 0.0
        discharge_w = 0.0
        power_w = 0.0
    point = {
        "ts_ms": int(ts_ms),
        "date": date_str,
        "soc_pct": round(float(soc_pct), 2),
        "power_w": round(
            float(power_w if not mode.startswith("discharge_") else -power_w), 1
        ),
        "charge_fc_w": round(charge_w, 1),
        "discharge_fc_w": round(discharge_w, 1),
    }
    if (
        state.trace
        and int(ts_ms) - int(state.trace[-1].get("ts_ms", 0))
        < TRACE_MIN_INTERVAL_S * 1000
    ):
        state.trace[-1] = point
    else:
        state.trace.append(point)
    cutoff_ms = int(ts_ms - TRACE_RETENTION_DAYS * 86400 * 1000)
    state.trace = compact_virtual_trace(
        [item for item in state.trace if item.get("ts_ms", 0) >= cutoff_ms]
    )[-TRACE_MAX_POINTS:]

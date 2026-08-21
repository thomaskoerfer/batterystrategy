"""Primitives shared by Battery Strategy layer contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

SLOT_MS = 15 * 60 * 1000
CONTRACT_SCHEMA_VERSION = 2


class QualityFlag(StrEnum):
    """Machine-readable reasons why a slot is not fully observed."""

    ESTIMATED = "estimated"
    MISSING_GRID = "missing_grid"
    MISSING_PV = "missing_pv"
    MISSING_BATTERY = "missing_battery"
    MISSING_EV = "missing_ev"
    MISSING_PRICE = "missing_price"
    COUNTER_RESET = "counter_reset"
    RESTART_GAP = "restart_gap"
    COMPONENT_MISMATCH = "component_mismatch"


@dataclass(frozen=True, slots=True)
class DataQuality:
    """Coverage and provenance attached to measured or forecast data."""

    coverage: float = 1.0
    flags: tuple[QualityFlag, ...] = ()

    def __post_init__(self) -> None:
        _require_range("coverage", self.coverage, 0.0, 1.0)
        if len(set(self.flags)) != len(self.flags):
            raise ValueError("quality flags must be unique")


@dataclass(frozen=True, slots=True, order=True)
class SlotKey:
    """One canonical half-open 15-minute UTC interval."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0:
            raise ValueError("slot start_ms must be non-negative")
        if self.end_ms - self.start_ms != SLOT_MS:
            raise ValueError("slots must be exactly 15 minutes")
        if self.start_ms % SLOT_MS != 0:
            raise ValueError("slot start_ms must align to a UTC quarter-hour")


def require_nonnegative(name: str, value: float) -> None:
    """Validate a finite non-negative numeric contract value."""
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")


def require_finite(name: str, value: float) -> None:
    """Validate a finite numeric contract value with either sign."""
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")


def require_percentage(name: str, value: float) -> None:
    """Validate a percentage in the inclusive 0..100 range."""
    _require_range(name, value, 0.0, 100.0)


def require_slots_sorted_unique(slots: tuple[SlotKey, ...]) -> None:
    """Validate a non-empty, strictly ordered and contiguous slot grid."""
    if not slots:
        raise ValueError("at least one slot is required")
    if tuple(sorted(set(slots))) != slots:
        raise ValueError("slots must be sorted and unique")
    if any(
        previous.end_ms != current.start_ms
        for previous, current in zip(slots, slots[1:])
    ):
        raise ValueError("slots must form a contiguous 15-minute grid")


def _require_range(name: str, value: float, low: float, high: float) -> None:
    numeric = float(value)
    if not math.isfinite(numeric) or not low <= numeric <= high:
        raise ValueError(f"{name} must be between {low} and {high}")

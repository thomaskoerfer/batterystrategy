"""Shared immutable forecast history and target primitives."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LegacyForecastSample:
    """Immutable normalized history sample used by extracted forecasters."""

    ts_s: float
    load_w: float
    pv_w: float
    grid_import_w: float
    grid_export_w: float
    load_valid: bool = True
    pv_valid: bool = True

    @classmethod
    def from_mapping(cls, sample: dict) -> LegacyForecastSample:
        """Normalize the fields consumed by the production forecast."""
        return cls(
            ts_s=float(sample.get("ts", 0.0) or 0.0),
            load_w=float(sample.get("load_w", 0.0) or 0.0),
            pv_w=max(0.0, float(sample.get("pv_w", 0.0) or 0.0)),
            grid_import_w=float(sample.get("grid_import_w", 0.0) or 0.0),
            grid_export_w=float(sample.get("grid_export_w", 0.0) or 0.0),
        )

    @property
    def has_valid_live_power(self) -> bool:
        return not (
            self.load_w <= 1.0
            and self.grid_import_w <= 1.0
            and self.grid_export_w <= 1.0
            and self.pv_w <= 1.0
        )


@dataclass(frozen=True, slots=True)
class LegacyForecastTarget:
    """One requested slot and its normalized weather factor."""

    local_start: dt.datetime
    weather_factor: float

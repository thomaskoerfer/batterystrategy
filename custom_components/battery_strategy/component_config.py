"""HA-independent configuration primitives for load-component models."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

DEFAULT_DHW_ALLOWED_WINDOWS = "00:00-05:00,09:00-17:00"


@dataclass(frozen=True, slots=True)
class LoadComponentSpec:
    """Pure model selection data derived from one config subentry."""

    component_key: str
    profile: str
    allowed_windows: str = ""


def time_allowed(value: dt.datetime, windows: str) -> bool:
    """Return whether local time is inside any configured half-open window."""
    minute = value.hour * 60 + value.minute
    for part in windows.split(","):
        try:
            start_raw, end_raw = part.strip().split("-", 1)
            start = _clock_minutes(start_raw)
            end = _clock_minutes(end_raw)
        except ValueError:
            continue
        if start <= end and start <= minute < end:
            return True
        if start > end and (minute >= start or minute < end):
            return True
    return False


def validate_allowed_windows(value: str) -> bool:
    """Validate compact comma-separated daily half-open windows."""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        return False
    try:
        for part in parts:
            start, end = part.split("-", 1)
            _clock_minutes(start)
            _clock_minutes(end)
    except ValueError:
        return False
    return True


def _clock_minutes(value: str) -> int:
    hour_raw, minute_raw = value.strip().split(":", 1)
    hour, minute = int(hour_raw), int(minute_raw)
    if not 0 <= hour <= 24 or not 0 <= minute < 60 or (hour == 24 and minute != 0):
        raise ValueError("invalid clock time")
    return 0 if hour == 24 else hour * 60 + minute

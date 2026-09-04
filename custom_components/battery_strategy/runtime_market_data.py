"""Typed tariff schedule captured at the Home Assistant adapter seam."""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class TariffInterval:
    """One normalized retail tariff interval."""

    starts_at: dt.datetime
    price_eur_per_kwh: float
    source: str = "tibber"

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None:
            raise ValueError("tariff timestamps must be timezone-aware")
        if not math.isfinite(float(self.price_eur_per_kwh)):
            raise ValueError("tariff prices must be finite")

    @property
    def timestamp(self) -> float:
        return self.starts_at.timestamp()


@dataclass(frozen=True, slots=True)
class TariffSchedule:
    """Immutable normalized tariff observations for one planning snapshot."""

    intervals: tuple[TariffInterval, ...] = ()

    def __post_init__(self) -> None:
        intervals = tuple(self.intervals)
        if any(not isinstance(item, TariffInterval) for item in intervals):
            raise TypeError("tariff schedule requires TariffInterval values")
        object.__setattr__(self, "intervals", intervals)

    @classmethod
    def from_provider_rows(
        cls,
        rows: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
        timezone: dt.tzinfo,
    ) -> TariffSchedule:
        """Detach and normalize provider-specific aliases, units and duplicates."""
        merged: dict[float, TariffInterval] = {}
        for item in rows:
            try:
                value = float(
                    item.get("price_per_kwh", item.get("price", item.get("total")))
                )
                timestamp = (
                    item.get("start_time") or item.get("startsAt") or item.get("start")
                )
                parsed = dt.datetime.fromisoformat(
                    str(timestamp).replace("Z", "+00:00")
                )
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone)
                else:
                    parsed = parsed.astimezone(timezone)
                price_eur = value / 100.0 if value >= 2.0 else value
                if not math.isfinite(price_eur):
                    continue
                merged[parsed.timestamp()] = TariffInterval(parsed, price_eur)
            except AttributeError, TypeError, ValueError:
                continue
        return cls(tuple(merged[key] for key in sorted(merged)))

    def for_dates(self, date_set: set[str]) -> tuple[TariffInterval, ...]:
        return tuple(
            item
            for item in self.intervals
            if item.starts_at.date().isoformat() in date_set
        )

    def price_eur_at(self, timestamp: float) -> float | None:
        starts = [item.timestamp for item in self.intervals]
        position = bisect.bisect_right(starts, float(timestamp)) - 1
        if position < 0:
            return None
        return self.intervals[position].price_eur_per_kwh

    def future_price_stats(self, now_local: dt.datetime) -> Mapping[str, float] | None:
        date_set = {
            now_local.date().isoformat(),
            (now_local.date() + dt.timedelta(days=1)).isoformat(),
        }
        future = [
            item.price_eur_per_kwh * 100.0
            for item in self.for_dates(date_set)
            if item.starts_at >= now_local
        ]
        if not future:
            return None
        return MappingProxyType({"min_ct": min(future), "max_ct": max(future)})

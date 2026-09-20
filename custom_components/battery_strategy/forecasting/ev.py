"""Causal EV charging marginals, separate from house-load forecasting."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import (
    DataQuality,
    EvForecast,
    EvForecastSlot,
    ForecastRequest,
    HistoricalFeatureSlot,
    QualityFlag,
    QuantileEnergy,
)

MODEL_VERSION = "historical-ev-session-v1"
SLOT_H = 0.25
_EV_INVALID_FLAGS = frozenset({QualityFlag.MISSING_EV, QualityFlag.RESTART_GAP})


@dataclass(frozen=True, slots=True)
class HistoricalEvForecaster:
    """Condition weekly EV marginals on the captured active state."""

    current_power_w: float
    active_threshold_w: float

    def forecast(
        self,
        request: ForecastRequest,
        history: tuple[HistoricalFeatureSlot, ...],
    ) -> EvForecast:
        zone = ZoneInfo(request.timezone)
        eligible = tuple(
            item
            for item in history
            if item.slot.end_ms <= request.as_of_ms
            and item.quality.coverage >= 0.999
            and not (frozenset(item.quality.flags) & _EV_INVALID_FLAGS)
        )
        by_local = {_local_key(item.slot.start_ms, zone): item for item in eligible}
        targets = tuple(
            dt.datetime.fromtimestamp(slot.start_ms / 1000.0, dt.UTC).astimezone(zone)
            for slot in request.slots
        )
        active_kwh = max(0.0, self.active_threshold_w) / 1000.0 * SLOT_H
        currently_active = self.current_power_w >= self.active_threshold_w
        compatible_weeks = []
        for weeks_ago in range(1, 54):
            first = by_local.get(_shifted_key(targets[0], weeks_ago))
            if first is None:
                continue
            if (first.ev_charge_kwh >= active_kwh) == currently_active:
                compatible_weeks.append(weeks_ago)

        slots = []
        for index, (slot, target) in enumerate(
            zip(request.slots, targets, strict=True)
        ):
            unconditional = tuple(
                item.ev_charge_kwh
                for weeks_ago in range(1, 54)
                if (item := by_local.get(_shifted_key(target, weeks_ago))) is not None
            )
            conditioned = tuple(
                item.ev_charge_kwh
                for weeks_ago in compatible_weeks
                if (item := by_local.get(_shifted_key(target, weeks_ago))) is not None
            )
            values = conditioned or unconditional
            naive_probability = _active_probability(unconditional, active_kwh)
            probability = _active_probability(values, active_kwh)
            energy = _energy_distribution(values)
            if index == 0 and currently_active:
                current_kwh = max(0.0, self.current_power_w) / 1000.0 * SLOT_H
                energy = QuantileEnergy(current_kwh, current_kwh, current_kwh, 1)
                probability = 1.0
            quality = (
                DataQuality() if values else DataQuality(0.0, (QualityFlag.ESTIMATED,))
            )
            slots.append(
                EvForecastSlot(
                    slot,
                    energy,
                    probability,
                    naive_probability,
                    quality,
                )
            )
        cutoff = max((item.slot.end_ms for item in eligible), default=0)
        return EvForecast(
            f"ev-{request.as_of_ms}",
            request.as_of_ms,
            min(request.as_of_ms, cutoff),
            MODEL_VERSION,
            tuple(slots),
        )


def empty_ev_forecast(request: ForecastRequest, training_cutoff_ms: int) -> EvForecast:
    """Return an explicit unavailable-EV marginal for offline baseline callers."""
    quality = DataQuality(0.0, (QualityFlag.ESTIMATED,))
    return EvForecast(
        f"ev-unavailable-{request.as_of_ms}",
        request.as_of_ms,
        min(request.as_of_ms, max(0, training_cutoff_ms)),
        "ev-unavailable-v1",
        tuple(
            EvForecastSlot(slot, QuantileEnergy(0.0), 0.0, 0.0, quality)
            for slot in request.slots
        ),
    )


def _energy_distribution(values: tuple[float, ...]) -> QuantileEnergy:
    finite = tuple(sorted(value for value in values if math.isfinite(value)))
    if not finite:
        return QuantileEnergy(0.0)
    if len(finite) < 4:
        return QuantileEnergy(_quantile(finite, 0.5))
    return QuantileEnergy(
        _quantile(finite, 0.5),
        _quantile(finite, 0.1),
        _quantile(finite, 0.9),
        len(finite),
    )


def _active_probability(values: tuple[float, ...], threshold_kwh: float) -> float:
    if not values:
        return 0.0
    return sum(value >= threshold_kwh for value in values) / len(values)


def _quantile(values: tuple[float, ...], probability: float) -> float:
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _shifted_key(local: dt.datetime, weeks_ago: int) -> tuple[str, int, int, int]:
    shifted = local - dt.timedelta(weeks=weeks_ago)
    return shifted.date().isoformat(), shifted.hour, shifted.minute, shifted.fold


def _local_key(timestamp_ms: int, zone: ZoneInfo) -> tuple[str, int, int, int]:
    local = dt.datetime.fromtimestamp(timestamp_ms / 1000.0, dt.UTC).astimezone(zone)
    return local.date().isoformat(), local.hour, local.minute, local.fold

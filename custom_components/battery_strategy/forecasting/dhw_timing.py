"""Empirical next-cycle timing for domestic-hot-water recovery."""

from __future__ import annotations

import datetime as dt
import statistics
from bisect import bisect_left
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import ForecastRequest, HistoricalFeatureSlot, LoadComponentEnergy

_COMPONENT_KEY = "heat_pump_dhw"
_MIN_CYCLE_ENERGY_KWH = 0.02
_MIN_ACTIVE_FRACTION = 0.05


@dataclass(frozen=True, slots=True)
class _TriggerSample:
    temperature_c: float
    circulation_fraction: float | None
    local_minute: int
    weekend: bool
    minutes_to_cycle: float
    cycle_start_ms: int


def estimate_next_cycle_start(
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    temperature_c: float,
    circulation_fraction: float | None,
) -> int | None:
    """Return the nearest slot for the next cycle from comparable tank states."""
    samples = _trigger_samples(history, request)
    if not samples or not request.slots:
        return None
    local = dt.datetime.fromtimestamp(request.as_of_ms / 1000.0, dt.UTC).astimezone(
        ZoneInfo(request.timezone)
    )
    temperature_scale = _robust_scale([sample.temperature_c for sample in samples])

    def distance(sample: _TriggerSample) -> float:
        minute = local.hour * 60 + local.minute
        time_distance = (
            min(
                abs(sample.local_minute - minute),
                1440 - abs(sample.local_minute - minute),
            )
            / 180.0
        )
        circulation_distance = (
            abs(sample.circulation_fraction - circulation_fraction)
            if sample.circulation_fraction is not None
            and circulation_fraction is not None
            else 0.0
        )
        return (
            abs(sample.temperature_c - temperature_c) / temperature_scale
            + time_distance
            + (0.0 if sample.weekend == (local.weekday() >= 5) else 1.0)
            + circulation_distance
        )

    best_by_cycle: dict[int, _TriggerSample] = {}
    for sample in samples:
        previous = best_by_cycle.get(sample.cycle_start_ms)
        if previous is None or distance(sample) < distance(previous):
            best_by_cycle[sample.cycle_start_ms] = sample
    if len(best_by_cycle) < 3:
        return None
    nearest = sorted(best_by_cycle.values(), key=distance)[:3]
    minutes_to_cycle = statistics.median(sample.minutes_to_cycle for sample in nearest)
    predicted_ms = request.as_of_ms + round(minutes_to_cycle / 15.0) * 15 * 60_000
    if not request.slots[0].start_ms <= predicted_ms < request.slots[-1].end_ms:
        return None
    return min(
        range(len(request.slots)),
        key=lambda index: abs(request.slots[index].start_ms - predicted_ms),
    )


def _trigger_samples(
    history: tuple[HistoricalFeatureSlot, ...], request: ForecastRequest
) -> tuple[_TriggerSample, ...]:
    timezone = ZoneInfo(request.timezone)
    eligible = tuple(
        item for item in history[-90 * 96 :] if item.slot.end_ms <= request.as_of_ms
    )
    samples: list[_TriggerSample] = []
    segment: list[HistoricalFeatureSlot] = []
    for item in eligible:
        if segment and segment[-1].slot.end_ms != item.slot.start_ms:
            samples.extend(_segment_samples(segment, timezone))
            segment = []
        segment.append(item)
    samples.extend(_segment_samples(segment, timezone))
    return tuple(samples)


def _segment_samples(
    history: list[HistoricalFeatureSlot], timezone: ZoneInfo
) -> list[_TriggerSample]:
    cycle_starts = []
    was_active = False
    for item in history:
        is_active = _active(_component(item))
        if is_active and not was_active:
            cycle_starts.append(item.slot.start_ms)
        was_active = is_active

    samples = []
    for item in history:
        component = _component(item)
        if not _inactive(component):
            continue
        temperature_c = _feature(component, "dhw_temperature_c")
        if temperature_c is None:
            continue
        position = bisect_left(cycle_starts, item.slot.end_ms)
        if position >= len(cycle_starts):
            continue
        circulation = _feature(component, "circulation_fraction")
        local = dt.datetime.fromtimestamp(item.slot.end_ms / 1000.0, dt.UTC).astimezone(
            timezone
        )
        samples.append(
            _TriggerSample(
                temperature_c,
                (max(0.0, min(1.0, circulation)) if circulation is not None else None),
                local.hour * 60 + local.minute,
                local.weekday() >= 5,
                (cycle_starts[position] - item.slot.end_ms) / 60_000.0,
                cycle_starts[position],
            )
        )
    return samples


def _component(item: HistoricalFeatureSlot) -> LoadComponentEnergy | None:
    return next(
        (
            value
            for value in item.load_components
            if value.component_key == _COMPONENT_KEY
        ),
        None,
    )


def _usable(component: LoadComponentEnergy | None) -> bool:
    return bool(
        component is not None
        and component.quality.coverage >= 0.999
        and not component.quality.flags
    )


def _feature(component: LoadComponentEnergy | None, key: str) -> float | None:
    if not _usable(component):
        return None
    value = next((item for item in component.features if item.feature_key == key), None)
    if value is None or value.quality.coverage < 0.999 or value.quality.flags:
        return None
    return value.value


def _inactive(component: LoadComponentEnergy | None) -> bool:
    if not _usable(component):
        return False
    charging = _feature(component, "dhw_charging_fraction")
    return component.energy_kwh < _MIN_CYCLE_ENERGY_KWH and (
        charging is None or charging < _MIN_ACTIVE_FRACTION
    )


def _active(component: LoadComponentEnergy | None) -> bool:
    if not _usable(component):
        return False
    charging = _feature(component, "dhw_charging_fraction")
    return component.energy_kwh >= _MIN_CYCLE_ENERGY_KWH or (
        charging is not None and charging >= _MIN_ACTIVE_FRACTION
    )


def _robust_scale(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    return max(1.0, 1.4826 * mad)

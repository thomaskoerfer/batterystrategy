"""Pure enrichment of feature-store slots from Recorder component power."""

from __future__ import annotations

import bisect
from dataclasses import replace

from .contracts import (
    DataQuality,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    QualityFlag,
)
from .feature_store import MAX_CONTINUOUS_SAMPLE_GAP_MS


def backfill_component_power_history(
    history: tuple[HistoricalFeatureSlot, ...],
    series_by_component: dict[str, tuple[tuple[float, float | None], ...]],
) -> tuple[HistoricalFeatureSlot, ...]:
    """Return only slots enriched with previously absent component energy."""
    normalized = {
        key: _normalized_series(series)
        for key, series in series_by_component.items()
        if key and series
    }
    changed_slots = []
    for slot in history:
        components = {item.component_key: item for item in slot.load_components}
        added_keys: set[str] = set()
        changed = False
        for component_key, series in normalized.items():
            if component_key in components:
                continue
            energy_kwh = _slot_energy_kwh(
                series,
                slot.slot.start_ms / 1000.0,
                slot.slot.end_ms / 1000.0,
            )
            if energy_kwh is None:
                continue
            components[component_key] = LoadComponentEnergy(
                component_key,
                energy_kwh,
                DataQuality(),
            )
            added_keys.add(component_key)
            changed = True
        if not changed:
            continue
        ordered = tuple(components[key] for key in sorted(components))
        if sum(item.energy_kwh for item in ordered) > slot.house_load_no_ev_kwh + 1e-9:
            ordered = tuple(
                _with_mismatch(item) if item.component_key in added_keys else item
                for item in ordered
            )
        changed_slots.append(replace(slot, load_components=ordered))
    return tuple(changed_slots)


def merge_feature_history(
    current: tuple[HistoricalFeatureSlot, ...],
    update: tuple[HistoricalFeatureSlot, ...],
) -> tuple[HistoricalFeatureSlot, ...]:
    """Add missing background components without replacing current slot facts."""
    merged = {item.slot.start_ms: item for item in current}
    for candidate in update:
        item = merged.get(candidate.slot.start_ms)
        if item is None:
            merged[candidate.slot.start_ms] = candidate
            continue
        current_keys = {value.component_key for value in item.load_components}
        added = tuple(
            value
            for value in candidate.load_components
            if value.component_key not in current_keys
        )
        if added:
            combined = (*item.load_components, *added)
            mismatch = (
                sum(value.energy_kwh for value in combined)
                > item.house_load_no_ev_kwh + 1e-9
            )
            normalized_added = tuple(
                _with_component_mismatch(value, mismatch) for value in added
            )
            merged[candidate.slot.start_ms] = replace(
                item,
                load_components=tuple(
                    sorted(
                        (*item.load_components, *normalized_added),
                        key=lambda value: value.component_key,
                    )
                ),
            )
    return tuple(merged[key] for key in sorted(merged))


def _normalized_series(
    series: tuple[tuple[float, float | None], ...],
) -> tuple[tuple[float, float | None], ...]:
    by_timestamp: dict[float, float | None] = {}
    for timestamp_s, power_w in series:
        by_timestamp[float(timestamp_s)] = (
            None if power_w is None else max(0.0, float(power_w))
        )
    return tuple(sorted(by_timestamp.items()))


def _slot_energy_kwh(
    series: tuple[tuple[float, float | None], ...], start_s: float, end_s: float
) -> float | None:
    timestamps = tuple(item[0] for item in series)
    index = bisect.bisect_right(timestamps, start_s) - 1
    if index < 0:
        return None
    cursor = start_s
    power_w = series[index][1]
    if power_w is None:
        return None
    energy_kwh = 0.0
    index += 1
    while index < len(series) and series[index][0] < end_s:
        timestamp_s, next_power_w = series[index]
        if (
            power_w > 0.0
            and timestamp_s - cursor > MAX_CONTINUOUS_SAMPLE_GAP_MS / 1000.0
        ):
            return None
        if next_power_w is None:
            return None
        if timestamp_s > cursor:
            energy_kwh += power_w * (timestamp_s - cursor) / 3_600_000.0
            cursor = timestamp_s
        power_w = next_power_w
        index += 1
    if power_w > 0.0 and end_s - cursor > MAX_CONTINUOUS_SAMPLE_GAP_MS / 1000.0:
        return None
    energy_kwh += power_w * (end_s - cursor) / 3_600_000.0
    return max(0.0, energy_kwh)


def _with_mismatch(component: LoadComponentEnergy) -> LoadComponentEnergy:
    return _with_component_mismatch(component, True)


def _with_component_mismatch(
    component: LoadComponentEnergy, mismatch: bool
) -> LoadComponentEnergy:
    flags = tuple(
        sorted(
            {
                *(
                    flag
                    for flag in component.quality.flags
                    if flag != QualityFlag.COMPONENT_MISMATCH
                ),
                *((QualityFlag.COMPONENT_MISMATCH,) if mismatch else ()),
            },
            key=str,
        )
    )
    return replace(component, quality=DataQuality(component.quality.coverage, flags))

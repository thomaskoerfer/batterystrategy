"""Coupled domestic-hot-water and space-heating forecast adjustments."""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..component_config import time_allowed
from ..contracts import (
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadForecastContext,
)
from .history import ForecastTargetInput

SLOT_H = 0.25
MIN_ACTIVE_POWER_W = 100.0
MIN_CYCLE_ENERGY_KWH = 0.02
MIN_ACTIVE_FRACTION = 0.05


@dataclass(frozen=True, slots=True)
class HeatPumpComponentForecast:
    """Adjusted energy series for the two mutually exclusive heat-pump modes."""

    dhw_kwh: tuple[float, ...]
    space_heating_kwh: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _DhwCycle:
    total_energy_kwh: float
    active_hours: float
    start_temperature_c: float
    target_temperature_c: float
    peak_temperature_c: float
    outdoor_temperature_c: float | None
    local_start_minute: int
    weekend: bool

    @property
    def active_power_w(self) -> float:
        if self.active_hours <= 0:
            return 0.0
        return self.total_energy_kwh / self.active_hours * 1000.0


@dataclass(frozen=True, slots=True)
class _ObservedCycle:
    energy_kwh: float
    start_temperature_c: float | None
    local_start_minute: int
    weekend: bool
    accounted_through_ms: int | None


def adjust_heat_pump_forecast(
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    targets: tuple[ForecastTargetInput, ...],
    context: LoadForecastContext,
    allowed_windows: str,
    dhw_baseline_kwh: tuple[float, ...],
    heating_baseline_kwh: tuple[float, ...],
    forecast_outdoor_temperature_c: tuple[float | None, ...] = (),
) -> HeatPumpComponentForecast:
    """Apply finite DHW recovery and its physical effect on space heating."""
    dhw_driver = _driver(context, "heat_pump_dhw")
    temperature_c = _driver_feature(dhw_driver, "dhw_temperature_c")
    target_c = _driver_feature(dhw_driver, "dhw_target_c")
    hysteresis_k = _driver_feature(dhw_driver, "dhw_differential_c")
    charging = _driver_feature(dhw_driver, "dhw_charging_fraction")
    active_age_s = _driver_feature(dhw_driver, "dhw_active_age_s")
    active = bool(
        dhw_driver is not None
        and dhw_driver.quality.coverage > 0
        and (
            dhw_driver.power_w >= MIN_ACTIVE_POWER_W
            or (charging is not None and charging >= 0.5)
        )
    )

    dhw = list(dhw_baseline_kwh)
    historical_active_fraction = _historical_active_fractions(history, targets)
    active_fraction = list(historical_active_fraction)
    if (
        temperature_c is not None
        and target_c is not None
        and hysteresis_k is not None
        and hysteresis_k > 0
        and target_c > hysteresis_k
    ):
        cycles = _completed_cycles(history, request, target_c)
        observed = _observed_cycle(history, request)
        start_index = (
            0
            if active
            else _trigger_index(
                request,
                targets,
                allowed_windows,
                temperature_c,
                target_c - hysteresis_k,
            )
        )
        if start_index is not None and (
            active or temperature_c <= target_c - hysteresis_k
        ):
            remaining_slot_h = max(
                0.0,
                (
                    request.slots[start_index].end_ms
                    - max(request.as_of_ms, request.slots[start_index].start_ms)
                )
                / 3_600_000.0,
            )
            measured_power_w = dhw_driver.power_w if dhw_driver is not None else 0.0
            unfinalized_duration_h = (
                _unfinalized_cycle_duration_h(request, observed, active_age_s)
                if active and start_index == 0
                else 0.0
            )
            remaining_kwh, active_power_w = _remaining_cycle_energy(
                cycles,
                observed,
                temperature_c,
                target_c,
                hysteresis_k,
                _cycle_outdoor_temperature(
                    dhw_driver,
                    forecast_outdoor_temperature_c,
                    start_index,
                    active,
                ),
                measured_power_w,
                remaining_slot_h,
                observed.weekend,
                unfinalized_duration_h,
            )
            allocated, fractions, last_index = _allocate_cycle(
                request,
                targets,
                allowed_windows,
                start_index,
                remaining_kwh,
                active_power_w,
                respect_allowed_windows=not active,
            )
            if last_index is not None:
                baseline_end = _historical_cycle_end(
                    start_index,
                    dhw_baseline_kwh,
                    historical_active_fraction,
                )
                replacement_end = max(last_index, baseline_end)
                for index in range(start_index, replacement_end + 1):
                    dhw[index] = allocated[index]
                    active_fraction[index] = fractions[index]

    heating = _couple_space_heating(
        history,
        context,
        tuple(active_fraction),
        historical_active_fraction,
        heating_baseline_kwh,
    )
    return HeatPumpComponentForecast(tuple(dhw), heating)


def _remaining_cycle_energy(
    cycles: tuple[_DhwCycle, ...],
    observed: _ObservedCycle,
    temperature_c: float,
    target_c: float,
    hysteresis_k: float,
    outdoor_temperature_c: float | None,
    measured_power_w: float,
    remaining_slot_h: float,
    weekend: bool,
    unfinalized_duration_h: float,
) -> tuple[float, float]:
    if not cycles:
        # With no mature cycle evidence, contain live persistence to the current slot.
        return measured_power_w * remaining_slot_h / 1000.0, measured_power_w

    start_temperature_c = observed.start_temperature_c
    if start_temperature_c is None:
        start_temperature_c = min(temperature_c, target_c - hysteresis_k)
    start_delta_k = max(0.1, target_c - start_temperature_c)
    comparable = _nearest_cycles(
        cycles,
        start_delta_k,
        outdoor_temperature_c,
        observed.local_start_minute,
        weekend,
    )
    learned_power_w = statistics.median(
        cycle.active_power_w for cycle in comparable if cycle.active_power_w > 0
    )
    unfinalized_energy_kwh = learned_power_w * unfinalized_duration_h / 1000.0
    tail_k = statistics.median(
        max(
            0.0,
            cycle.peak_temperature_c - max(cycle.target_temperature_c, target_c),
        )
        for cycle in comparable
    )
    specific_energy = statistics.median(
        cycle.total_energy_kwh
        / max(0.1, cycle.peak_temperature_c - cycle.start_temperature_c)
        for cycle in comparable
    )
    total_kwh = specific_energy * max(0.0, target_c + tail_k - start_temperature_c)
    temperature_remaining_kwh = specific_energy * max(
        0.0, target_c + tail_k - temperature_c
    )
    thermally_delivered_kwh = specific_energy * max(
        0.0, temperature_c - start_temperature_c
    )
    remaining_kwh = max(
        max(0.0, temperature_remaining_kwh - unfinalized_energy_kwh),
        total_kwh
        - max(observed.energy_kwh + unfinalized_energy_kwh, thermally_delivered_kwh),
    )
    if measured_power_w < MIN_ACTIVE_POWER_W:
        active_power_w = learned_power_w
    elif observed.energy_kwh < MIN_CYCLE_ENERGY_KWH:
        # The current reading may still be on the compressor ramp even when a
        # tiny part of the cycle landed in the preceding finalized slot. Use
        # learned cycle power for duration without suppressing a higher live rate.
        active_power_w = max(measured_power_w, learned_power_w)
    else:
        active_power_w = measured_power_w
    return max(0.0, remaining_kwh), max(MIN_ACTIVE_POWER_W, active_power_w)


def _unfinalized_cycle_duration_h(
    request: ForecastRequest,
    observed: _ObservedCycle,
    active_age_s: float | None,
) -> float:
    """Return active time not represented by finalized cycle energy."""
    active_start_ms = None
    if active_age_s is not None:
        active_start_ms = request.as_of_ms - max(0.0, active_age_s) * 1000.0
    unaccounted_start_ms = observed.accounted_through_ms
    if unaccounted_start_ms is None:
        unaccounted_start_ms = active_start_ms
    elif active_start_ms is not None:
        unaccounted_start_ms = max(unaccounted_start_ms, active_start_ms)
    if unaccounted_start_ms is None:
        return 0.0
    return min(
        SLOT_H,
        max(0.0, request.as_of_ms - unaccounted_start_ms) / 3_600_000.0,
    )


def _nearest_cycles(
    cycles: tuple[_DhwCycle, ...],
    start_delta_k: float,
    outdoor_temperature_c: float | None,
    local_start_minute: int,
    weekend: bool,
) -> tuple[_DhwCycle, ...]:
    deltas = [
        cycle.target_temperature_c - cycle.start_temperature_c for cycle in cycles
    ]
    oats = [
        cycle.outdoor_temperature_c
        for cycle in cycles
        if cycle.outdoor_temperature_c is not None
    ]
    delta_scale = _robust_scale(deltas)
    oat_scale = _robust_scale(oats)

    def distance(cycle: _DhwCycle) -> float:
        delta_distance = (
            abs(
                (cycle.target_temperature_c - cycle.start_temperature_c) - start_delta_k
            )
            / delta_scale
        )
        oat_distance = 0.0
        if (
            outdoor_temperature_c is not None
            and cycle.outdoor_temperature_c is not None
        ):
            oat_distance = (
                abs(cycle.outdoor_temperature_c - outdoor_temperature_c) / oat_scale
            )
        elif outdoor_temperature_c is not None:
            oat_distance = 1.0
        time_distance = (
            min(
                abs(cycle.local_start_minute - local_start_minute),
                1440 - abs(cycle.local_start_minute - local_start_minute),
            )
            / 180.0
        )
        day_type_distance = 0.0 if cycle.weekend == weekend else 1.0
        return delta_distance + oat_distance + time_distance + day_type_distance

    count = max(3, min(12, math.ceil(math.sqrt(len(cycles)) * 2)))
    return tuple(sorted(cycles, key=distance)[:count])


def _robust_scale(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    return max(1.0, 1.4826 * mad)


def _allocate_cycle(
    request: ForecastRequest,
    targets: tuple[ForecastTargetInput, ...],
    allowed_windows: str,
    start_index: int,
    remaining_kwh: float,
    active_power_w: float,
    *,
    respect_allowed_windows: bool,
) -> tuple[list[float], list[float], int | None]:
    energy = [0.0] * len(request.slots)
    fractions = [0.0] * len(request.slots)
    remaining = remaining_kwh
    last_index = start_index if remaining <= 1e-9 else None
    for index in range(start_index, len(request.slots)):
        if remaining <= 1e-9 or (
            respect_allowed_windows
            and not time_allowed(targets[index].local_start, allowed_windows)
        ):
            break
        slot = request.slots[index]
        active_start_ms = (
            max(slot.start_ms, request.as_of_ms) if index == 0 else slot.start_ms
        )
        available_h = max(0.0, (slot.end_ms - active_start_ms) / 3_600_000.0)
        capacity_kwh = active_power_w / 1000.0 * available_h
        supplied = min(remaining, capacity_kwh)
        energy[index] = supplied
        fractions[index] = supplied / max(active_power_w / 1000.0 * SLOT_H, 1e-9)
        remaining -= supplied
        last_index = index
    return energy, fractions, last_index


def _trigger_index(
    request: ForecastRequest,
    targets: tuple[ForecastTargetInput, ...],
    allowed_windows: str,
    temperature_c: float,
    cut_in_c: float,
) -> int | None:
    if temperature_c > cut_in_c:
        return None
    for index, target in enumerate(targets):
        if request.slots[index].end_ms <= request.as_of_ms:
            continue
        if time_allowed(target.local_start, allowed_windows):
            return index
    return None


def _completed_cycles(
    history: tuple[HistoricalFeatureSlot, ...],
    request: ForecastRequest,
    target_c: float,
) -> tuple[_DhwCycle, ...]:
    groups: list[list[tuple[HistoricalFeatureSlot, LoadComponentEnergy]]] = []
    current: list[tuple[HistoricalFeatureSlot, LoadComponentEnergy]] = []
    for item in history[-90 * 96 :]:
        component = _component(item, "heat_pump_dhw")
        active = _component_usable(component) and (
            component.energy_kwh >= MIN_CYCLE_ENERGY_KWH
            or (_reliable_feature(component.features, "dhw_charging_fraction") or 0.0)
            >= MIN_ACTIVE_FRACTION
        )
        contiguous = not current or current[-1][0].slot.end_ms == item.slot.start_ms
        if active and contiguous:
            current.append((item, component))
            continue
        if current:
            groups.append(current)
            current = []
        if active:
            current = [(item, component)]
    # A group touching the history tail may still be active and is not training data.
    if current and current[-1][0].slot.end_ms < request.as_of_ms - 15 * 60 * 1000:
        groups.append(current)

    result = []
    timezone = ZoneInfo(request.timezone)
    for group in groups:
        temperatures = [
            value
            for _, component in group
            if (value := _reliable_feature(component.features, "dhw_temperature_c"))
            is not None
        ]
        if not temperatures:
            continue
        total_energy = sum(component.energy_kwh for _, component in group)
        charging_fractions = [
            value
            for _, component in group
            if (value := _reliable_feature(component.features, "dhw_charging_fraction"))
            is not None
        ]
        if charging_fractions:
            active_hours = sum(
                max(0.0, min(1.0, value)) * SLOT_H for value in charging_fractions
            )
        else:
            peak_slot_kwh = max(component.energy_kwh for _, component in group)
            active_hours = (
                total_energy / (peak_slot_kwh / SLOT_H) if peak_slot_kwh > 0 else 0.0
            )
        if active_hours <= 0 or total_energy < MIN_CYCLE_ENERGY_KWH:
            continue
        oats = [
            value
            for _, component in group
            if (value := _reliable_feature(component.features, "outdoor_temperature_c"))
            is not None
        ]
        targets = [
            value
            for _, component in group
            if (value := _reliable_feature(component.features, "dhw_target_c"))
            is not None
        ]
        cycle_target_c = statistics.median(targets) if targets else target_c
        local = dt.datetime.fromtimestamp(
            group[0][0].slot.start_ms / 1000.0, dt.UTC
        ).astimezone(timezone)
        result.append(
            _DhwCycle(
                total_energy,
                active_hours,
                min(temperatures),
                cycle_target_c,
                max(max(temperatures), cycle_target_c),
                statistics.mean(oats) if oats else None,
                local.hour * 60 + local.minute,
                local.weekday() >= 5,
            )
        )
    return tuple(result)


def _observed_cycle(
    history: tuple[HistoricalFeatureSlot, ...], request: ForecastRequest
) -> _ObservedCycle:
    current = []
    for item in reversed(history):
        if item.slot.end_ms > request.as_of_ms:
            continue
        component = _component(item, "heat_pump_dhw")
        active = _component_usable(component) and (
            component.energy_kwh >= MIN_CYCLE_ENERGY_KWH
            or (_reliable_feature(component.features, "dhw_charging_fraction") or 0.0)
            >= MIN_ACTIVE_FRACTION
        )
        if not active:
            break
        if current and item.slot.end_ms != current[-1][0].slot.start_ms:
            break
        current.append((item, component))
    current.reverse()
    temperatures = [
        value
        for _, component in current
        if (value := _reliable_feature(component.features, "dhw_temperature_c"))
        is not None
    ]
    if current:
        local = dt.datetime.fromtimestamp(
            current[0][0].slot.start_ms / 1000.0, dt.UTC
        ).astimezone(ZoneInfo(request.timezone))
        minute = local.hour * 60 + local.minute
        weekend = local.weekday() >= 5
    else:
        local = dt.datetime.fromtimestamp(request.as_of_ms / 1000.0, dt.UTC).astimezone(
            ZoneInfo(request.timezone)
        )
        minute = local.hour * 60 + local.minute
        weekend = local.weekday() >= 5
    return _ObservedCycle(
        sum(component.energy_kwh for _, component in current),
        min(temperatures) if temperatures else None,
        minute,
        weekend,
        current[-1][0].slot.end_ms if current else None,
    )


def _historical_active_fractions(history, targets) -> list[float]:
    if not targets:
        return []
    timezone = targets[0].local_start.tzinfo
    buckets: dict[tuple[int, bool], list[float]] = {}
    for item in history[-90 * 96 :]:
        component = _component(item, "heat_pump_dhw")
        if not _component_usable(component):
            continue
        fraction = _reliable_feature(component.features, "dhw_charging_fraction")
        if fraction is None:
            fraction = 1.0 if component.energy_kwh >= MIN_CYCLE_ENERGY_KWH else 0.0
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, dt.UTC
        ).astimezone(timezone)
        key = (local.hour * 4 + local.minute // 15, local.weekday() >= 5)
        buckets.setdefault(key, []).append(max(0.0, min(1.0, fraction)))
    return [
        float(statistics.median(buckets.get(key, ())[-12:]))
        if buckets.get(key)
        else 0.0
        for target in targets
        for key in [
            (
                target.local_start.hour * 4 + target.local_start.minute // 15,
                target.local_start.weekday() >= 5,
            )
        ]
    ]


def _couple_space_heating(
    history,
    context,
    predicted_dhw_fraction,
    historical_fraction,
    heating_baseline_kwh,
) -> tuple[float, ...]:
    heating = [0.0] * len(heating_baseline_kwh)
    deferred_kwh = 0.0
    heating_driver = _driver(context, "heat_pump_space_heating")
    reference_power_w = max(
        _historical_active_power(history, "heat_pump_space_heating"),
        heating_driver.power_w if heating_driver is not None else 0.0,
        max(heating_baseline_kwh, default=0.0) / SLOT_H * 1000.0,
    )
    if reference_power_w <= 0:
        return heating_baseline_kwh

    for index, baseline in enumerate(heating_baseline_kwh):
        predicted = max(0.0, min(1.0, predicted_dhw_fraction[index]))
        historical = max(0.0, min(1.0, historical_fraction[index]))
        slot_capacity = reference_power_w / 1000.0 * SLOT_H
        occupancy_delta = predicted - historical
        if occupancy_delta >= 0:
            served = max(0.0, baseline - slot_capacity * occupancy_delta)
        else:
            free_capacity = max(0.0, slot_capacity * (1.0 - predicted) - baseline)
            served = baseline + min(
                slot_capacity * -occupancy_delta,
                free_capacity,
            )
        heating[index] = served
        carry = deferred_kwh
        deferred_kwh += baseline - served
        if predicted > 0 or abs(carry) <= 1e-9:
            continue
        capacity = slot_capacity
        if carry > 0:
            recovery = min(carry, max(0.0, capacity - heating[index]))
            heating[index] += recovery
            deferred_kwh -= recovery
        else:
            avoided_recovery = min(-carry, heating[index])
            heating[index] -= avoided_recovery
            deferred_kwh += avoided_recovery
    return tuple(heating)


def _historical_active_power(history, key: str) -> float:
    values = []
    for item in history[-90 * 96 :]:
        component = _component(item, key)
        if (
            not _component_usable(component)
            or component.energy_kwh < MIN_CYCLE_ENERGY_KWH
        ):
            continue
        values.append(component.energy_kwh / SLOT_H * 1000.0)
    return float(statistics.median(values)) if values else 0.0


def _driver(context, key: str):
    return next((item for item in context.drivers if item.driver_key == key), None)


def _component(item, key: str):
    return next(
        (value for value in item.load_components if value.component_key == key), None
    )


def _feature(features, key: str):
    item = next((item for item in features if item.feature_key == key), None)
    if item is None or item.quality.coverage < 0.999 or item.quality.flags:
        return None
    return item.value


def _driver_feature(driver, key: str):
    return _feature(driver.features, key) if driver is not None else None


def _cycle_outdoor_temperature(
    driver,
    forecast: tuple[float | None, ...],
    start_index: int,
    active: bool,
) -> float | None:
    current = _driver_feature(driver, "outdoor_temperature_c")
    if active or start_index >= len(forecast):
        return current
    return forecast[start_index] if forecast[start_index] is not None else current


def _historical_cycle_end(
    start_index: int,
    baseline_kwh: tuple[float, ...],
    active_fraction: list[float],
) -> int:
    end = start_index
    for index in range(start_index, len(baseline_kwh)):
        if (
            baseline_kwh[index] < MIN_CYCLE_ENERGY_KWH
            and active_fraction[index] < MIN_ACTIVE_FRACTION
        ):
            break
        end = index
    return end


def _component_usable(component) -> bool:
    return bool(
        component is not None
        and component.quality.coverage >= 0.999
        and not component.quality.flags
    )


def _reliable_feature(features, key: str):
    item = next((item for item in features if item.feature_key == key), None)
    if item is None or item.quality.coverage < 0.999 or item.quality.flags:
        return None
    return item.value

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
from .dhw_timing import estimate_next_cycle_start
from .history import ForecastTargetInput

SLOT_H = 0.25
MIN_ACTIVE_POWER_W = 100.0
MIN_CYCLE_ENERGY_KWH = 0.02
MIN_ACTIVE_FRACTION = 0.05
MIN_TIMING_CORRECTION_SLOTS = 2


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
    circulation_fraction: float | None
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

    historical_active_fraction = _historical_active_fractions(history, targets)
    dhw = list(dhw_baseline_kwh)
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
        if cycles:
            dhw, active_fraction = _project_next_thermal_cycle(
                request,
                targets,
                context,
                allowed_windows,
                cycles,
                observed,
                history,
                temperature_c,
                target_c,
                hysteresis_k,
                active,
                active_age_s,
                forecast_outdoor_temperature_c,
                tuple(dhw),
                tuple(active_fraction),
            )
        else:
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
            if start_index is None or (
                not active and temperature_c > target_c - hysteresis_k
            ):
                start_index = None
        if not cycles and start_index is not None:
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
                _driver_feature(dhw_driver, "circulation_fraction"),
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


def _project_next_thermal_cycle(
    request: ForecastRequest,
    targets: tuple[ForecastTargetInput, ...],
    context: LoadForecastContext,
    allowed_windows: str,
    cycles: tuple[_DhwCycle, ...],
    observed: _ObservedCycle,
    history: tuple[HistoricalFeatureSlot, ...],
    temperature_c: float,
    target_c: float,
    hysteresis_k: float,
    active: bool,
    active_age_s: float | None,
    forecast_outdoor_temperature_c: tuple[float | None, ...],
    baseline_kwh: tuple[float, ...],
    baseline_active_fraction: tuple[float, ...],
) -> tuple[list[float], list[float]]:
    """Refine the next historical cycle with current thermal evidence."""
    energy = list(baseline_kwh)
    fractions = list(baseline_active_fraction)
    dhw_driver = _driver(context, "heat_pump_dhw")
    baseline_cycle = _next_baseline_cycle(baseline_kwh, baseline_active_fraction)

    if active:
        remaining_slot_h = max(
            0.0,
            (request.slots[0].end_ms - request.as_of_ms) / 3_600_000.0,
        )
        outdoor_temperature_c = _cycle_outdoor_temperature(
            dhw_driver,
            forecast_outdoor_temperature_c,
            0,
            True,
        )
        remaining_kwh, active_power_w = _remaining_cycle_energy(
            cycles,
            observed,
            temperature_c,
            target_c,
            hysteresis_k,
            outdoor_temperature_c,
            dhw_driver.power_w if dhw_driver is not None else 0.0,
            remaining_slot_h,
            observed.weekend,
            _unfinalized_cycle_duration_h(request, observed, active_age_s),
            _driver_feature(dhw_driver, "circulation_fraction"),
        )
        allocated, allocated_fractions, last_index = _allocate_cycle(
            request,
            targets,
            allowed_windows,
            0,
            remaining_kwh,
            active_power_w,
            respect_allowed_windows=False,
        )
        replacement_end = last_index if last_index is not None else 0
        search_from = 0
        while cycle := _next_baseline_cycle(
            baseline_kwh,
            baseline_active_fraction,
            search_from,
        ):
            if cycle[0] > replacement_end:
                break
            replacement_end = max(replacement_end, cycle[1])
            search_from = cycle[1] + 1
        for clear_index in range(replacement_end + 1):
            energy[clear_index] = 0.0
            fractions[clear_index] = 0.0
        _copy_allocation(energy, fractions, allocated, allocated_fractions)
        return energy, fractions

    cut_in_c = target_c - hysteresis_k
    start_index = _trigger_index(
        request,
        targets,
        allowed_windows,
        temperature_c,
        cut_in_c,
    )
    if temperature_c > cut_in_c:
        if baseline_cycle is None:
            return energy, fractions
        start_index = estimate_next_cycle_start(
            request,
            history,
            temperature_c,
            _driver_feature(dhw_driver, "circulation_fraction"),
        )
        if start_index is None:
            return energy, fractions
        if not time_allowed(targets[start_index].local_start, allowed_windows):
            return energy, fractions
        if abs(start_index - baseline_cycle[0]) < MIN_TIMING_CORRECTION_SLOTS:
            start_index = baseline_cycle[0]
    if start_index is None:
        return energy, fractions

    outdoor_temperature_c = (
        forecast_outdoor_temperature_c[start_index]
        if start_index < len(forecast_outdoor_temperature_c)
        else None
    )
    cycle_start_temperature_c = min(temperature_c, cut_in_c)
    specific_energy, tail_k, active_power_w = _cycle_characteristics(
        cycles,
        max(0.1, target_c - cycle_start_temperature_c),
        outdoor_temperature_c,
        targets[start_index].local_start.hour * 60
        + targets[start_index].local_start.minute,
        targets[start_index].local_start.weekday() >= 5,
        target_c,
        _driver_feature(dhw_driver, "circulation_fraction"),
    )
    required_kwh = specific_energy * max(
        0.0, target_c + tail_k - cycle_start_temperature_c
    )
    if baseline_cycle is not None:
        allocated, allocated_fractions, allocated_end = _scale_baseline_cycle(
            baseline_kwh,
            baseline_cycle,
            start_index,
            required_kwh,
            active_power_w,
        )
    else:
        allocated, allocated_fractions, allocated_end = _allocate_cycle(
            request,
            targets,
            allowed_windows,
            start_index,
            required_kwh,
            active_power_w,
            respect_allowed_windows=False,
        )
    if baseline_cycle is not None:
        following_cycle = _next_baseline_cycle(
            baseline_kwh,
            baseline_active_fraction,
            baseline_cycle[1] + 1,
        )
        if (
            following_cycle is not None
            and allocated_end is not None
            and allocated_end >= following_cycle[0]
        ):
            return energy, fractions
        for clear_index in range(baseline_cycle[0], baseline_cycle[1] + 1):
            energy[clear_index] = 0.0
            fractions[clear_index] = 0.0
    _copy_allocation(energy, fractions, allocated, allocated_fractions)
    return energy, fractions


def _next_baseline_cycle(
    baseline_kwh: tuple[float, ...],
    baseline_active_fraction: tuple[float, ...],
    start_at: int = 0,
) -> tuple[int, int] | None:
    energy_start = next(
        (
            index
            for index in range(start_at, len(baseline_kwh))
            if baseline_kwh[index] >= MIN_CYCLE_ENERGY_KWH
        ),
        None,
    )
    start = energy_start
    if start is not None:
        while (
            start > start_at
            and baseline_active_fraction[start - 1] >= MIN_ACTIVE_FRACTION
        ):
            start -= 1
    else:
        start = next(
            (
                index
                for index in range(start_at, len(baseline_active_fraction))
                if baseline_active_fraction[index] >= MIN_ACTIVE_FRACTION
            ),
            None,
        )
    if start is None:
        return None
    end = start
    for index in range(start + 1, len(baseline_kwh)):
        if (
            baseline_kwh[index] < MIN_CYCLE_ENERGY_KWH
            and baseline_active_fraction[index] < MIN_ACTIVE_FRACTION
        ):
            break
        end = index
    return start, end


def _copy_allocation(
    target_energy: list[float],
    target_fractions: list[float],
    energy: list[float],
    fractions: list[float],
) -> None:
    for index, value in enumerate(energy):
        if value <= 0.0:
            continue
        target_energy[index] = value
        target_fractions[index] = fractions[index]


def _scale_baseline_cycle(
    baseline_kwh: tuple[float, ...],
    baseline_cycle: tuple[int, int],
    start_index: int,
    required_kwh: float,
    active_power_w: float,
) -> tuple[list[float], list[float], int | None]:
    energy = [0.0] * len(baseline_kwh)
    fractions = [0.0] * len(baseline_kwh)
    source_start = next(
        (
            index
            for index in range(baseline_cycle[0], baseline_cycle[1] + 1)
            if baseline_kwh[index] >= MIN_CYCLE_ENERGY_KWH
        ),
        baseline_cycle[0],
    )
    baseline_total = sum(baseline_kwh[source_start : baseline_cycle[1] + 1])
    if baseline_total <= 0 or required_kwh <= 0:
        return energy, fractions, None
    scale = required_kwh / baseline_total
    slot_capacity_kwh = max(
        active_power_w * SLOT_H / 1000.0,
        max(baseline_kwh[source_start : baseline_cycle[1] + 1]),
    )
    last_index = None
    remaining = 0.0
    source_values = baseline_kwh[source_start : baseline_cycle[1] + 1]
    for offset in range(len(energy) - start_index):
        source_value = source_values[offset] if offset < len(source_values) else 0.0
        remaining += source_value * scale
        target_index = start_index + offset
        if target_index >= len(energy):
            break
        supplied = min(remaining, slot_capacity_kwh)
        if supplied <= 0 and offset >= len(source_values):
            break
        energy[target_index] = supplied
        fractions[target_index] = min(1.0, supplied / slot_capacity_kwh)
        remaining -= supplied
        last_index = target_index
        if remaining <= 1e-9 and offset >= len(source_values) - 1:
            break
    return energy, fractions, last_index


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
    circulation_fraction: float | None = None,
) -> tuple[float, float]:
    if not cycles:
        # With no mature cycle evidence, contain live persistence to the current slot.
        return measured_power_w * remaining_slot_h / 1000.0, measured_power_w

    start_temperature_c = observed.start_temperature_c
    if start_temperature_c is None:
        start_temperature_c = min(temperature_c, target_c - hysteresis_k)
    start_delta_k = max(0.1, target_c - start_temperature_c)
    specific_energy, tail_k, learned_power_w = _cycle_characteristics(
        cycles,
        start_delta_k,
        outdoor_temperature_c,
        observed.local_start_minute,
        weekend,
        target_c,
        circulation_fraction,
    )
    unfinalized_energy_kwh = learned_power_w * unfinalized_duration_h / 1000.0
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


def _cycle_characteristics(
    cycles: tuple[_DhwCycle, ...],
    start_delta_k: float,
    outdoor_temperature_c: float | None,
    local_start_minute: int,
    weekend: bool,
    target_c: float,
    circulation_fraction: float | None = None,
) -> tuple[float, float, float]:
    comparable = _nearest_cycles(
        cycles,
        start_delta_k,
        outdoor_temperature_c,
        local_start_minute,
        weekend,
        circulation_fraction,
    )
    weights = tuple(weight for _, weight in comparable)
    specific_energy = _weighted_mean(
        tuple(
            cycle.total_energy_kwh
            / max(0.1, cycle.peak_temperature_c - cycle.start_temperature_c)
            for cycle, _ in comparable
        ),
        weights,
    )
    tail_k = _weighted_median(
        tuple(
            max(
                0.0,
                cycle.peak_temperature_c - max(cycle.target_temperature_c, target_c),
            )
            for cycle, _ in comparable
        ),
        weights,
    )
    power_samples = tuple(
        (cycle.active_power_w, weight)
        for cycle, weight in comparable
        if cycle.active_power_w > 0
    )
    active_power_w = _weighted_median(
        tuple(value for value, _ in power_samples),
        tuple(weight for _, weight in power_samples),
    )
    return specific_energy, tail_k, active_power_w


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
    circulation_fraction: float | None,
) -> tuple[tuple[_DhwCycle, float], ...]:
    deltas = [cycle.peak_temperature_c - cycle.start_temperature_c for cycle in cycles]
    oats = [
        cycle.outdoor_temperature_c
        for cycle in cycles
        if cycle.outdoor_temperature_c is not None
    ]
    delta_scale = _robust_scale(deltas)
    oat_scale = _robust_scale(oats)

    def distance(cycle: _DhwCycle) -> float:
        delta_distance = (
            abs((cycle.peak_temperature_c - cycle.start_temperature_c) - start_delta_k)
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
        circulation_distance = 0.0
        if circulation_fraction is not None and cycle.circulation_fraction is not None:
            circulation_distance = abs(
                cycle.circulation_fraction - circulation_fraction
            )
        return (
            delta_distance
            + oat_distance
            + time_distance
            + day_type_distance
            + circulation_distance
        )

    count = max(3, min(12, math.ceil(math.sqrt(len(cycles)) * 2)))
    nearest = sorted(cycles, key=distance)[:count]
    return tuple((cycle, 1.0 / (1.0 + distance(cycle)) ** 2) for cycle in nearest)


def _weighted_median(values: tuple[float, ...], weights: tuple[float, ...]) -> float:
    ordered = sorted(zip(values, weights, strict=True))
    midpoint = sum(weight for _, weight in ordered) / 2.0
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= midpoint:
            return value
    return ordered[-1][0]


def _weighted_mean(values: tuple[float, ...], weights: tuple[float, ...]) -> float:
    total_weight = sum(weights)
    if total_weight <= 0:
        return statistics.fmean(values)
    return (
        sum(value * weight for value, weight in zip(values, weights, strict=True))
        / total_weight
    )


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
    groups: list[
        tuple[
            list[tuple[HistoricalFeatureSlot, LoadComponentEnergy]],
            tuple[HistoricalFeatureSlot, LoadComponentEnergy] | None,
            tuple[HistoricalFeatureSlot, LoadComponentEnergy],
        ]
    ] = []
    current: list[tuple[HistoricalFeatureSlot, LoadComponentEnergy]] = []
    preceding: tuple[HistoricalFeatureSlot, LoadComponentEnergy] | None = None
    cycle_preceding: tuple[HistoricalFeatureSlot, LoadComponentEnergy] | None = None
    cycle_end_ms: int | None = None
    cycle_valid = False
    for item in history[-90 * 96 :]:
        component = _component(item, "heat_pump_dhw")
        activity = _cycle_activity(component)
        if current and cycle_end_ms != item.slot.start_ms:
            current = []
            cycle_preceding = None
            cycle_end_ms = None
            cycle_valid = False
            preceding = None
        if current:
            cycle_end_ms = item.slot.end_ms
            if activity is True:
                if component is not None:
                    current.append((item, component))
                cycle_valid = cycle_valid and _component_usable(component)
                continue
            if activity is None:
                cycle_valid = False
                continue
            if cycle_valid and component is not None and _component_usable(component):
                groups.append((current, cycle_preceding, (item, component)))
            current = []
            cycle_preceding = None
            cycle_end_ms = None
            cycle_valid = False
        if activity is True and component is not None:
            current = [(item, component)]
            cycle_preceding = (
                preceding
                if preceding is not None
                and preceding[0].slot.end_ms == item.slot.start_ms
                else None
            )
            cycle_end_ms = item.slot.end_ms
            cycle_valid = _component_usable(component)
            preceding = None
        elif (
            activity is False and component is not None and _component_usable(component)
        ):
            preceding = (item, component)
        else:
            preceding = None

    result = []
    timezone = ZoneInfo(request.timezone)
    for group, before, after in groups:
        active_temperatures = [
            value
            for _, component in group
            if (value := _reliable_feature(component.features, "dhw_temperature_c"))
            is not None
        ]
        before_temperature = (
            _reliable_feature(before[1].features, "dhw_temperature_c")
            if before is not None
            else None
        )
        after_temperature = _reliable_feature(after[1].features, "dhw_temperature_c")
        if not active_temperatures:
            continue
        total_energy = sum(component.energy_kwh for _, component in group)
        charging_fractions = [
            _reliable_feature(component.features, "dhw_charging_fraction")
            for _, component in group
        ]
        if any(value is not None for value in charging_fractions):
            active_hours = sum(
                max(0.0, min(1.0, value if value is not None else 1.0)) * SLOT_H
                for value in charging_fractions
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
        circulation = [
            value
            for _, component in group
            if (value := _reliable_feature(component.features, "circulation_fraction"))
            is not None
        ]
        cycle_target_c = statistics.median(targets) if targets else target_c
        start_temperature_c = min(
            active_temperatures[0],
            before_temperature
            if before_temperature is not None
            else active_temperatures[0],
        )
        peak_temperature_c = max(
            *active_temperatures,
            after_temperature
            if after_temperature is not None
            else active_temperatures[-1],
        )
        if peak_temperature_c < target_c:
            continue
        local = dt.datetime.fromtimestamp(
            group[0][0].slot.start_ms / 1000.0, dt.UTC
        ).astimezone(timezone)
        result.append(
            _DhwCycle(
                total_energy,
                active_hours,
                start_temperature_c,
                cycle_target_c,
                peak_temperature_c,
                statistics.mean(oats) if oats else None,
                statistics.mean(circulation) if circulation else None,
                local.hour * 60 + local.minute,
                local.weekday() >= 5,
            )
        )
    return tuple(result)


def _cycle_activity(component: LoadComponentEnergy | None) -> bool | None:
    if component is None:
        return None
    charging = _reliable_feature(component.features, "dhw_charging_fraction")
    if charging is not None and charging >= MIN_ACTIVE_FRACTION:
        return True
    if _component_usable(component):
        return component.energy_kwh >= MIN_CYCLE_ENERGY_KWH
    if charging is not None:
        return False
    return None


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

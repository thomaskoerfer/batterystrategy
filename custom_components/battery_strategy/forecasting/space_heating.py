"""Production space-heating forecast coupled to domestic hot water demand."""

from __future__ import annotations

import datetime as dt
import heapq
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import (
    DataQuality,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadForecastContext,
    QualityFlag,
    WeatherSlot,
)

SLOT_H = 0.25
HEATING_KEY = "heat_pump_space_heating"
DHW_KEY = "heat_pump_dhw"
MODEL_VERSION = "space-heating-v2"
MAX_HISTORY_SLOTS = 90 * 96
MAX_NEIGHBORS = 96
MIN_ACTIVE_KWH = 0.025


@dataclass(frozen=True, slots=True)
class SpaceHeatingPointForecast:
    """Point forecast and per-slot quality before residual calibration."""

    energy_kwh: tuple[float, ...]
    quality: tuple[DataQuality, ...]
    model_version: str = MODEL_VERSION


@dataclass(frozen=True, slots=True)
class _HeatingSample:
    start_ms: int
    energy_kwh: float
    outdoor_temperature_c: float | None
    target_flow_temperature_c: float | None
    dhw_occupancy: float
    dhw_active_slot_kwh: float | None
    heating_active_slot_kwh: float | None
    local_slot: int
    weekend: bool


def forecast_space_heating(
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    context: LoadForecastContext,
    weather: tuple[WeatherSlot, ...],
    dhw_kwh: tuple[float, ...],
) -> SpaceHeatingPointForecast:
    """Forecast space-heating energy with shared-compressor DHW coupling."""
    eligible = tuple(item for item in history if item.slot.end_ms <= request.as_of_ms)
    samples = _heating_samples(eligible, request.timezone)
    global_dhw_active_slot_kwh = _mean_present(
        sample.dhw_active_slot_kwh for sample in samples
    )
    global_heating_active_slot_kwh = _mean_present(
        sample.heating_active_slot_kwh for sample in samples
    )
    driver = _driver(context, HEATING_KEY)
    current_active = bool(
        driver is not None
        and driver.quality.coverage > 0
        and (
            driver.power_w > 100.0
            or (_driver_feature(driver, "heating_active_fraction") or 0.0) >= 0.5
        )
    )
    active_age_s = max(0.0, _driver_feature(driver, "heating_active_age_s") or 0.0)
    current_power_w = driver.power_w if driver is not None else 0.0
    weather_by_slot = {item.slot: item for item in weather}
    timezone = ZoneInfo(request.timezone)
    raw_energy = []
    raw_quality = []
    historical_dhw_occupancy = []
    dhw_active_slot_kwh = []
    heating_active_slot_kwh = []
    live_heating_kwh = []
    for index, slot in enumerate(request.slots):
        local = dt.datetime.fromtimestamp(slot.start_ms / 1000.0, dt.UTC).astimezone(
            timezone
        )
        weather_slot = weather_by_slot.get(slot)
        target_oat = (
            weather_slot.temperature_c
            if weather_slot is not None
            and weather_slot.quality.coverage > 0
            and not weather_slot.quality.flags
            else _driver_feature(driver, "outdoor_temperature_c")
        )
        target_flow = _driver_feature(driver, "target_flow_temperature_c")
        neighbors = _nearest_samples(samples, local, target_oat, target_flow)
        (
            expected_kwh,
            _active_probability,
            baseline_dhw_occupancy,
            active_dhw_kwh,
            active_heating_kwh,
        ) = _distribution(neighbors)
        active_dhw_kwh = active_dhw_kwh or global_dhw_active_slot_kwh
        active_heating_kwh = active_heating_kwh or global_heating_active_slot_kwh
        live_energy = 0.0
        if current_active:
            elapsed_slots = active_age_s / (SLOT_H * 3600.0) + index
            persistence = _survival_probability(samples, elapsed_slots)
            live_energy = current_power_w * SLOT_H / 1000.0 * persistence
            active_heating_kwh = max(
                active_heating_kwh or 0.0,
                current_power_w * SLOT_H / 1000.0,
            )
        raw_energy.append(max(0.0, expected_kwh))
        raw_quality.append(
            DataQuality() if neighbors else DataQuality(0.0, (QualityFlag.ESTIMATED,))
        )
        historical_dhw_occupancy.append(baseline_dhw_occupancy)
        dhw_active_slot_kwh.append(active_dhw_kwh)
        heating_active_slot_kwh.append(active_heating_kwh)
        live_heating_kwh.append(live_energy)

    energy, quality = _couple_with_dhw(
        tuple(raw_energy),
        tuple(raw_quality),
        dhw_kwh,
        tuple(historical_dhw_occupancy),
        tuple(dhw_active_slot_kwh),
        tuple(heating_active_slot_kwh),
        tuple(live_heating_kwh),
    )
    return SpaceHeatingPointForecast(energy, quality)


def _heating_samples(
    history: tuple[HistoricalFeatureSlot, ...], timezone: str
) -> tuple[_HeatingSample, ...]:
    zone = ZoneInfo(timezone)
    result = []
    for item in history[-MAX_HISTORY_SLOTS:]:
        component = next(
            (
                candidate
                for candidate in item.load_components
                if candidate.component_key == HEATING_KEY
            ),
            None,
        )
        if (
            component is None
            or component.quality.coverage < 0.999
            or component.quality.flags
        ):
            continue
        dhw = next(
            (
                candidate
                for candidate in item.load_components
                if candidate.component_key == DHW_KEY
            ),
            None,
        )
        dhw_occupancy = (
            min(
                1.0,
                max(0.0, _feature(dhw.features, "dhw_charging_fraction") or 0.0),
            )
            if dhw is not None
            else 0.0
        )
        dhw_active_slot_kwh = (
            dhw.energy_kwh / dhw_occupancy
            if dhw is not None and dhw.energy_kwh > 1e-9 and dhw_occupancy > 0.05
            else None
        )
        historically_available = max(0.0, 1.0 - dhw_occupancy)
        heating_active_slot_kwh = (
            component.energy_kwh / historically_available
            if component.energy_kwh >= MIN_ACTIVE_KWH and historically_available > 0.05
            else None
        )
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, dt.UTC
        ).astimezone(zone)
        result.append(
            _HeatingSample(
                item.slot.start_ms,
                component.energy_kwh,
                _feature(component.features, "outdoor_temperature_c"),
                _feature(component.features, "target_flow_temperature_c"),
                dhw_occupancy,
                dhw_active_slot_kwh,
                heating_active_slot_kwh,
                local.hour * 4 + local.minute // 15,
                local.weekday() >= 5,
            )
        )
    return tuple(result)


def _nearest_samples(samples, local, target_oat, target_flow):
    target_slot = local.hour * 4 + local.minute // 15
    weekend = local.weekday() >= 5

    def ranked_sample(sample):
        slot_distance = abs(sample.local_slot - target_slot)
        slot_distance = min(slot_distance, 96 - slot_distance)
        distance = slot_distance / 8.0
        if sample.weekend != weekend:
            distance += 0.75
        if target_oat is not None and sample.outdoor_temperature_c is not None:
            distance += abs(sample.outdoor_temperature_c - target_oat) / 2.0
        if target_flow is not None and sample.target_flow_temperature_c is not None:
            distance += abs(sample.target_flow_temperature_c - target_flow) / 2.0
        return distance, sample

    return tuple(
        heapq.nsmallest(
            MAX_NEIGHBORS,
            (ranked_sample(sample) for sample in samples),
            key=lambda item: (item[0], -item[1].start_ms),
        )
    )


def _distribution(neighbors):
    if not neighbors:
        return 0.0, 0.0, 0.0, None, None
    weighted = tuple(
        (math.exp(-min(20.0, distance)), sample.energy_kwh)
        for distance, sample in neighbors
    )
    total_weight = sum(weight for weight, _ in weighted)
    expected = sum(weight * value for weight, value in weighted) / total_weight
    active_probability = (
        sum(weight for weight, value in weighted if value >= MIN_ACTIVE_KWH)
        / total_weight
    )
    dhw_occupancy = (
        sum(
            weight * sample.dhw_occupancy
            for (weight, _), (_, sample) in zip(weighted, neighbors, strict=True)
        )
        / total_weight
    )
    active_dhw = tuple(
        (math.exp(-min(20.0, distance)), sample.dhw_active_slot_kwh)
        for distance, sample in neighbors
        if sample.dhw_active_slot_kwh is not None
    )
    active_dhw_kwh = (
        sum(weight * value for weight, value in active_dhw)
        / sum(weight for weight, _ in active_dhw)
        if active_dhw
        else None
    )
    active_heating = tuple(
        (math.exp(-min(20.0, distance)), sample.heating_active_slot_kwh)
        for distance, sample in neighbors
        if sample.heating_active_slot_kwh is not None
    )
    active_heating_kwh = (
        sum(weight * value for weight, value in active_heating)
        / sum(weight for weight, _ in active_heating)
        if active_heating
        else None
    )
    return (
        expected,
        active_probability,
        dhw_occupancy,
        active_dhw_kwh,
        active_heating_kwh,
    )


def _survival_probability(samples, elapsed_slots):
    completed_durations = []
    current = 0
    previous_start = None
    for sample in sorted(samples, key=lambda item: item.start_ms):
        contiguous = (
            previous_start is not None and sample.start_ms - previous_start == 900_000
        )
        if sample.energy_kwh >= MIN_ACTIVE_KWH:
            current = current + 1 if contiguous else 1
        elif current:
            completed_durations.append(current)
            current = 0
        previous_start = sample.start_ms
    censored_duration = current or None
    if not completed_durations and censored_duration is None:
        return math.exp(-elapsed_slots / 4.0)
    if not completed_durations:
        return math.exp(-max(0.0, elapsed_slots - censored_duration) / 4.0)
    observations = tuple((duration, True) for duration in completed_durations)
    if censored_duration is not None:
        observations += ((censored_duration, False),)
    survival = 1.0
    for duration in sorted(set(completed_durations)):
        if duration > elapsed_slots:
            break
        at_risk = sum(value >= duration for value, _ in observations)
        events = sum(
            value == duration and completed for value, completed in observations
        )
        if at_risk:
            survival *= 1.0 - events / at_risk
    return survival


def _couple_with_dhw(
    heating_kwh,
    heating_quality,
    dhw_kwh,
    historical_dhw_occupancy,
    dhw_active_slot_kwh,
    heating_active_slot_kwh,
    live_heating_kwh,
):
    result = []
    quality_result = []
    deferred = 0.0
    reference_capacity = max(heating_kwh, default=0.0)
    for (
        heating,
        quality,
        dhw,
        baseline_occupancy,
        active_dhw_kwh,
        active_heating_kwh,
        live_kwh,
    ) in zip(
        heating_kwh,
        heating_quality,
        dhw_kwh,
        historical_dhw_occupancy,
        dhw_active_slot_kwh,
        heating_active_slot_kwh,
        live_heating_kwh,
        strict=True,
    ):
        missing_dhw_capacity = active_dhw_kwh is None and dhw > 1e-9
        predicted_occupancy = (
            min(1.0, dhw / max(0.001, active_dhw_kwh))
            if active_dhw_kwh is not None
            else float(dhw > 1e-9)
        )
        incremental_occupancy = max(0.0, predicted_occupancy - baseline_occupancy)
        historically_available = max(1e-6, 1.0 - baseline_occupancy)
        displaced_fraction = min(1.0, incremental_occupancy / historically_available)
        baseline_served = heating * (1.0 - displaced_fraction)
        deferred += heating - baseline_served
        live_served = live_kwh * (1.0 - predicted_occupancy)
        served = max(baseline_served, live_served)
        available_capacity = (
            active_heating_kwh * (1.0 - predicted_occupancy)
            if active_heating_kwh is not None
            else reference_capacity
        )
        served = min(served, available_capacity)
        if predicted_occupancy < 1e-9:
            recovery = min(deferred, max(0.0, available_capacity - served))
            served += recovery
            deferred -= recovery
        result.append(served)
        quality_result.append(
            _quality_with_flag(quality, QualityFlag.ESTIMATED)
            if missing_dhw_capacity
            else quality
        )
    return tuple(result), tuple(quality_result)


def _mean_present(values):
    present = tuple(value for value in values if value is not None)
    return sum(present) / len(present) if present else None


def _quality_with_flag(quality, flag):
    return DataQuality(quality.coverage, tuple(dict.fromkeys((*quality.flags, flag))))


def _driver(context, key):
    return next((item for item in context.drivers if item.driver_key == key), None)


def _feature(features, key):
    item = next((item for item in features if item.feature_key == key), None)
    if item is None or item.quality.coverage < 0.999 or item.quality.flags:
        return None
    return item.value


def _driver_feature(driver, key):
    return _feature(driver.features, key) if driver is not None else None

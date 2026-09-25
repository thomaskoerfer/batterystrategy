"""Non-authoritative whole-heat-pump forecast candidate.

The candidate is deliberately isolated from forecast composition. It consumes the
same immutable inputs as production forecasting and may only be persisted by the
evaluation layer.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import (
    DataQuality,
    ForecastRequest,
    ForecastSlot,
    HistoricalFeatureSlot,
    LoadForecast,
    LoadForecastComponent,
    LoadForecastContext,
    QualityFlag,
    QuantileEnergy,
    WeatherSlot,
)

SLOT_H = 0.25
HEATING_KEY = "heat_pump_space_heating"
DHW_KEY = "heat_pump_dhw"
MODEL_VERSION = "whole-heat-pump-shadow-v1"
MAX_HISTORY_SLOTS = 90 * 96
MAX_NEIGHBORS = 96
MIN_ACTIVE_KWH = 0.025


@dataclass(frozen=True, slots=True)
class HeatPumpShadowForecast:
    """Evaluation-only forecast for DHW and space-heating electricity."""

    generated_at_ms: int
    training_cutoff_ms: int
    model_version: str
    components: tuple[LoadForecastComponent, ...]
    total_slots: tuple[ForecastSlot, ...]
    diagnostics: dict[str, object]
    non_authoritative: bool = True

    def component(self, key: str) -> LoadForecastComponent:
        """Return one named heat-pump component."""
        return next(item for item in self.components if item.component_key == key)


@dataclass(frozen=True, slots=True)
class HeatPumpShadowRequest:
    """Immutable inputs captured with one authoritative forecast vintage."""

    request: ForecastRequest
    history: tuple[HistoricalFeatureSlot, ...]
    context: LoadForecastContext
    weather: tuple[WeatherSlot, ...]
    authoritative_load: LoadForecast


def evaluate_heat_pump_shadow(
    candidate: HeatPumpShadowRequest,
) -> HeatPumpShadowForecast:
    """Evaluate a captured candidate after authoritative publication."""
    return build_heat_pump_shadow_forecast(
        candidate.request,
        candidate.history,
        candidate.context,
        candidate.weather,
        candidate.authoritative_load,
    )


@dataclass(frozen=True, slots=True)
class _HeatingSample:
    start_ms: int
    energy_kwh: float
    outdoor_temperature_c: float | None
    target_flow_temperature_c: float | None
    local_slot: int
    weekend: bool


def build_heat_pump_shadow_forecast(
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    context: LoadForecastContext,
    weather: tuple[WeatherSlot, ...],
    authoritative_load: LoadForecast,
) -> HeatPumpShadowForecast:
    """Build a whole-WP candidate without modifying authoritative output."""
    dhw = _component(authoritative_load, DHW_KEY)
    production_heating = _component(authoritative_load, HEATING_KEY)
    if dhw is None or production_heating is None:
        raise ValueError("whole heat-pump shadow requires both heat-pump components")

    eligible = tuple(item for item in history if item.slot.end_ms <= request.as_of_ms)
    training_cutoff_ms = max((item.slot.end_ms for item in eligible), default=0)
    samples = _heating_samples(eligible, request.timezone)
    driver = _driver(context, HEATING_KEY)
    current_active = bool(
        driver is not None
        and driver.quality.coverage > 0
        and (
            driver.power_w > 100.0
            or (_driver_feature(driver, "heating_active_fraction") or 0.0) >= 0.5
        )
    )
    active_age_s = max(
        0.0, _driver_feature(driver, "heating_active_age_s") or 0.0
    )
    current_power_w = driver.power_w if driver is not None else 0.0
    weather_by_slot = {item.slot: item for item in weather}
    timezone = ZoneInfo(request.timezone)
    raw_slots: list[ForecastSlot] = []
    active_probabilities: list[float] = []
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
        expected_kwh, p10_kwh, p90_kwh, active_probability = _distribution(neighbors)
        if current_active:
            elapsed_slots = active_age_s / (SLOT_H * 3600.0) + index
            persistence = _survival_probability(samples, elapsed_slots)
            live_energy = current_power_w * SLOT_H / 1000.0 * persistence
            expected_kwh = max(expected_kwh, live_energy)
            active_probability = max(active_probability, persistence)
            p90_kwh = max(p90_kwh, current_power_w * SLOT_H / 1000.0)
        sample_count = len(neighbors)
        energy = (
            QuantileEnergy(
                max(0.0, expected_kwh),
                max(0.0, min(p10_kwh, expected_kwh)),
                max(expected_kwh, p90_kwh),
                sample_count,
            )
            if sample_count
            else QuantileEnergy(max(0.0, expected_kwh))
        )
        raw_slots.append(
            ForecastSlot(
                slot,
                energy,
                DataQuality()
                if sample_count
                else DataQuality(0.0, (QualityFlag.ESTIMATED,)),
            )
        )
        active_probabilities.append(active_probability)

    coupled_slots = _couple_with_dhw(tuple(raw_slots), dhw.slots)
    heating = LoadForecastComponent(
        HEATING_KEY,
        "space-heating-shadow-v1",
        min(request.as_of_ms, training_cutoff_ms),
        coupled_slots,
    )
    dhw_shadow = LoadForecastComponent(
        DHW_KEY,
        f"dhw-shadow-from-{dhw.model_version}",
        min(request.as_of_ms, dhw.training_cutoff_ms),
        dhw.slots,
    )
    total_slots = tuple(
        ForecastSlot(
            slot.slot,
            _sum_energy(slot.energy, heating.slots[index].energy),
            _combined_quality(slot.quality, heating.slots[index].quality),
        )
        for index, slot in enumerate(dhw_shadow.slots)
    )
    return HeatPumpShadowForecast(
        generated_at_ms=request.as_of_ms,
        training_cutoff_ms=min(request.as_of_ms, training_cutoff_ms),
        model_version=MODEL_VERSION,
        components=(dhw_shadow, heating),
        total_slots=total_slots,
        diagnostics={
            "status": "ready" if samples else "cold_start",
            "history_samples": len(samples),
            "current_heating_active": current_active,
            "current_active_age_s": round(active_age_s, 1),
            "active_probability": [round(value, 4) for value in active_probabilities],
        },
    )


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
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, dt.UTC
        ).astimezone(zone)
        result.append(
            _HeatingSample(
                item.slot.start_ms,
                component.energy_kwh,
                _feature(component.features, "outdoor_temperature_c"),
                _feature(component.features, "target_flow_temperature_c"),
                local.hour * 4 + local.minute // 15,
                local.weekday() >= 5,
            )
        )
    return tuple(result)


def _nearest_samples(samples, local, target_oat, target_flow):
    target_slot = local.hour * 4 + local.minute // 15
    weekend = local.weekday() >= 5
    ranked = []
    for sample in samples:
        slot_distance = abs(sample.local_slot - target_slot)
        slot_distance = min(slot_distance, 96 - slot_distance)
        distance = slot_distance / 8.0
        if sample.weekend != weekend:
            distance += 0.75
        if target_oat is not None and sample.outdoor_temperature_c is not None:
            distance += abs(sample.outdoor_temperature_c - target_oat) / 2.0
        if target_flow is not None and sample.target_flow_temperature_c is not None:
            distance += abs(sample.target_flow_temperature_c - target_flow) / 2.0
        ranked.append((distance, sample))
    ranked.sort(key=lambda item: (item[0], -item[1].start_ms))
    return tuple(ranked[:MAX_NEIGHBORS])


def _distribution(neighbors):
    if not neighbors:
        return 0.0, 0.0, 0.0, 0.0
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
    return (
        expected,
        _weighted_quantile(weighted, 0.10),
        _weighted_quantile(weighted, 0.90),
        active_probability,
    )


def _weighted_quantile(weighted, quantile):
    ordered = sorted(weighted, key=lambda item: item[1])
    threshold = sum(weight for weight, _ in ordered) * quantile
    cumulative = 0.0
    for weight, value in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][1]


def _survival_probability(samples, elapsed_slots):
    durations = []
    current = 0
    previous_start = None
    for sample in sorted(samples, key=lambda item: item.start_ms):
        contiguous = (
            previous_start is not None and sample.start_ms - previous_start == 900_000
        )
        if sample.energy_kwh >= MIN_ACTIVE_KWH:
            current = current + 1 if contiguous else 1
        elif current:
            durations.append(current)
            current = 0
        previous_start = sample.start_ms
    if current:
        durations.append(current)
    if not durations:
        return math.exp(-elapsed_slots / 4.0)
    remaining = sum(1 for duration in durations if duration > elapsed_slots)
    return remaining / len(durations)


def _couple_with_dhw(heating_slots, dhw_slots):
    result = []
    deferred = [0.0, 0.0, 0.0]
    reference_capacity = max(
        (item.energy.p90_kwh or item.energy.p50_kwh for item in heating_slots),
        default=0.0,
    )
    for heating, dhw in zip(heating_slots, dhw_slots, strict=True):
        calibrated = heating.energy.p10_kwh is not None
        quantiles = [
            heating.energy.p10_kwh or heating.energy.p50_kwh,
            heating.energy.p50_kwh,
            heating.energy.p90_kwh or heating.energy.p50_kwh,
        ]
        dhw_fraction = min(1.0, dhw.energy.p50_kwh / max(0.001, reference_capacity))
        coupled = []
        for index, value in enumerate(quantiles):
            served = value * (1.0 - dhw_fraction)
            deferred[index] += value - served
            recovery = min(deferred[index], max(0.0, reference_capacity - served))
            if dhw_fraction < 1e-9:
                served += recovery
                deferred[index] -= recovery
            coupled.append(served)
        result.append(
            ForecastSlot(
                heating.slot,
                QuantileEnergy(
                    coupled[1],
                    min(coupled[0], coupled[1]),
                    max(coupled[1], coupled[2]),
                    heating.energy.calibration_samples,
                )
                if calibrated
                else QuantileEnergy(coupled[1]),
                heating.quality,
            )
        )
    return tuple(result)


def _sum_energy(left, right):
    if left.p10_kwh is None or right.p10_kwh is None:
        return QuantileEnergy(left.p50_kwh + right.p50_kwh)
    return QuantileEnergy(
        left.p50_kwh + right.p50_kwh,
        left.p10_kwh + right.p10_kwh,
        left.p90_kwh + right.p90_kwh,
        min(left.calibration_samples, right.calibration_samples),
    )


def _combined_quality(left, right):
    return DataQuality(
        min(left.coverage, right.coverage),
        tuple(dict.fromkeys((*left.flags, *right.flags))),
    )


def _component(load, key):
    return next((item for item in load.components if item.component_key == key), None)


def _driver(context, key):
    return next((item for item in context.drivers if item.driver_key == key), None)


def _feature(features, key):
    item = next((item for item in features if item.feature_key == key), None)
    if item is None or item.quality.coverage < 0.999 or item.quality.flags:
        return None
    return item.value


def _driver_feature(driver, key):
    return _feature(driver.features, key) if driver is not None else None

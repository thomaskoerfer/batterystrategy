"""Composite EV-free load forecast with independent measured components."""

from __future__ import annotations

import datetime as dt
import statistics

from ..component_config import LoadComponentSpec, time_allowed
from ..const import LOAD_PROFILE_AIR_CONDITIONING, LOAD_PROFILE_HEAT_PUMP
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
from .heat_pump import adjust_heat_pump_forecast
from .history import ForecastHistorySample, ForecastTargetInput
from .load import LoadForecastModelConfig, build_load_forecast

SLOT_H = 0.25
MIN_COMPONENT_HISTORY_SLOTS = 7 * 96
_LOAD_INVALID_FLAGS = frozenset(
    {
        QualityFlag.MISSING_GRID,
        QualityFlag.MISSING_PV,
        QualityFlag.MISSING_BATTERY,
        QualityFlag.MISSING_EV,
        QualityFlag.RESTART_GAP,
    }
)
_PV_INVALID_FLAGS = frozenset({QualityFlag.MISSING_PV, QualityFlag.RESTART_GAP})


def build_component_load_forecast(
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    targets: tuple[ForecastTargetInput, ...],
    context: LoadForecastContext,
    weather: tuple[WeatherSlot, ...],
    specs: tuple[LoadComponentSpec, ...],
    config: LoadForecastModelConfig,
) -> LoadForecast:
    """Compose independently trained component forecasts and residual load."""
    if not specs:
        return build_load_forecast(
            request, _total_samples(history), targets, context, config
        )
    keys = tuple(spec.component_key for spec in specs)
    eligible = tuple(item for item in history if _has_components(item, keys))
    if len(eligible) < MIN_COMPONENT_HISTORY_SLOTS:
        # Do not add a partially learned component to a whole-house baseline: that
        # would double count it. Collection warms up without changing the baseline output.
        fallback = build_load_forecast(
            request, _total_samples(history), targets, context, config
        )
        quality = DataQuality(0.0, (QualityFlag.ESTIMATED,))
        zero_slots = tuple(
            ForecastSlot(slot, QuantileEnergy(0.0), quality) for slot in request.slots
        )
        general = LoadForecastComponent(
            "general_house_load",
            "component-warming-v1",
            fallback.training_cutoff_ms,
            fallback.slots,
        )
        return LoadForecast(
            f"component-warming-{request.as_of_ms}",
            request.as_of_ms,
            fallback.training_cutoff_ms,
            "component-load-warming-v1",
            fallback.slots,
            (
                general,
                *tuple(
                    LoadForecastComponent(
                        spec.component_key,
                        "component-warming-v1",
                        fallback.training_cutoff_ms,
                        zero_slots,
                    )
                    for spec in specs
                ),
            ),
        )

    residual = build_load_forecast(
        request,
        _residual_samples(eligible, keys),
        targets,
        _residual_context(context, keys),
        config,
    )
    weather_by_slot = {item.slot: item for item in weather}
    components = [
        LoadForecastComponent(
            "general_house_load",
            "residual-load-v1",
            residual.training_cutoff_ms,
            residual.slots,
        )
    ]
    component_energy: dict[str, tuple[float, ...]] = {}
    for spec in specs:
        component_energy[spec.component_key] = tuple(
            _component_power_w(
                spec,
                target.local_start,
                eligible,
                context,
                weather_by_slot.get(slot),
                request,
            )
            * SLOT_H
            / 1000.0
            for slot, target in zip(request.slots, targets, strict=True)
        )
    dhw_spec = next(
        (
            spec
            for spec in specs
            if spec.profile == LOAD_PROFILE_HEAT_PUMP
            and spec.component_key == "heat_pump_dhw"
        ),
        None,
    )
    if dhw_spec is not None and "heat_pump_space_heating" in component_energy:
        heat_pump = adjust_heat_pump_forecast(
            request,
            eligible,
            targets,
            context,
            dhw_spec.allowed_windows,
            component_energy["heat_pump_dhw"],
            component_energy["heat_pump_space_heating"],
            tuple(
                weather_by_slot.get(slot).temperature_c
                if weather_by_slot.get(slot) is not None
                and weather_by_slot.get(slot).quality.coverage >= 0.999
                and not weather_by_slot.get(slot).quality.flags
                else None
                for slot in request.slots
            ),
        )
        component_energy["heat_pump_dhw"] = heat_pump.dhw_kwh
        component_energy["heat_pump_space_heating"] = heat_pump.space_heating_kwh

    for spec in specs:
        component_slots = tuple(
            ForecastSlot(
                slot,
                QuantileEnergy(component_energy[spec.component_key][index]),
            )
            for index, slot in enumerate(request.slots)
        )
        components.append(
            LoadForecastComponent(
                spec.component_key,
                f"{spec.profile}-v4"
                if spec.profile == LOAD_PROFILE_HEAT_PUMP
                else f"{spec.profile}-v1",
                residual.training_cutoff_ms,
                component_slots,
            )
        )
    total_slots = tuple(
        ForecastSlot(
            slot,
            QuantileEnergy(
                sum(component.slots[index].energy.p50_kwh for component in components)
            ),
        )
        for index, slot in enumerate(request.slots)
    )
    return LoadForecast(
        f"component-{request.as_of_ms}",
        request.as_of_ms,
        residual.training_cutoff_ms,
        "component-load-v4"
        if any(spec.profile == LOAD_PROFILE_HEAT_PUMP for spec in specs)
        else "component-load-v1",
        total_slots,
        tuple(components),
    )


def _component_power_w(spec, target, history, context, weather, request) -> float:
    samples = []
    target_slot = target.hour * 4 + target.minute // 15
    target_weekend = target.weekday() >= 5
    target_oat = weather.temperature_c if weather is not None else None
    for item in history[-90 * 96 :]:
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, tz=dt.UTC
        ).astimezone(target.tzinfo)
        if local.hour * 4 + local.minute // 15 != target_slot:
            continue
        if (local.weekday() >= 5) != target_weekend:
            continue
        component = next(
            (
                value
                for value in item.load_components
                if value.component_key == spec.component_key
            ),
            None,
        )
        if (
            component is None
            or component.quality.coverage < 0.999
            or component.quality.flags
        ):
            continue
        sample_oat = _feature(component.features, "outdoor_temperature_c")
        distance = (
            abs(sample_oat - target_oat)
            if sample_oat is not None and target_oat is not None
            else 0.0
        )
        samples.append((distance, component.energy_kwh / SLOT_H * 1000.0))
    samples.sort(key=lambda item: item[0])
    base = statistics.median(value for _, value in samples[:12]) if samples else 0.0

    driver = next(
        (item for item in context.drivers if item.driver_key == spec.component_key),
        None,
    )
    horizon_h = max(0.0, (target.timestamp() * 1000 - request.as_of_ms) / 3_600_000.0)
    if spec.profile == LOAD_PROFILE_HEAT_PUMP and spec.component_key == "heat_pump_dhw":
        if not time_allowed(target, spec.allowed_windows):
            return 0.0
        return max(0.0, float(base))
    if spec.profile == LOAD_PROFILE_AIR_CONDITIONING and driver is not None:
        indoor = _driver_feature(driver, "indoor_temperature_max_c")
        target_indoor = _driver_feature(driver, "target_temperature_mean_c")
        if horizon_h <= 1.0 and driver.power_w > 0:
            return max(base, driver.power_w)
        if (
            horizon_h <= 3.0
            and indoor is not None
            and target_indoor is not None
            and indoor <= target_indoor
        ):
            return min(base, driver.power_w)
    return max(0.0, float(base))


def _total_samples(history) -> tuple[ForecastHistorySample, ...]:
    return tuple(_sample(item, item.house_load_no_ev_kwh) for item in history)


def _residual_samples(history, keys) -> tuple[ForecastHistorySample, ...]:
    return tuple(
        _sample(
            item,
            max(
                0.0,
                item.house_load_no_ev_kwh
                - sum(
                    component.energy_kwh
                    for component in item.load_components
                    if component.component_key in keys
                ),
            ),
        )
        for item in history
    )


def _sample(item, load_kwh) -> ForecastHistorySample:
    flags = frozenset(item.quality.flags)
    load_valid = item.quality.coverage >= 0.999 and not flags & _LOAD_INVALID_FLAGS
    pv_valid = item.quality.coverage >= 0.999 and not flags & _PV_INVALID_FLAGS
    return ForecastHistorySample(
        item.slot.start_ms / 1000.0,
        load_kwh / SLOT_H * 1000.0,
        item.pv_generation_kwh / SLOT_H * 1000.0,
        item.grid_import_kwh / SLOT_H * 1000.0,
        item.grid_export_kwh / SLOT_H * 1000.0,
        load_valid=load_valid,
        pv_valid=pv_valid,
    )


def _has_components(item, keys) -> bool:
    present = {component.component_key: component for component in item.load_components}
    return (
        item.quality.coverage >= 0.999
        and not frozenset(item.quality.flags) & _LOAD_INVALID_FLAGS
        and all(
            key in present
            and present[key].quality.coverage >= 0.999
            and not present[key].quality.flags
            for key in keys
        )
    )


def _residual_context(context, keys) -> LoadForecastContext:
    component_power = sum(
        driver.power_w
        for driver in context.drivers
        if driver.driver_key in keys and driver.quality.coverage > 0
    )
    return LoadForecastContext(max(0.0, context.house_load_no_ev_w - component_power))


def _feature(features, key):
    item = next((item for item in features if item.feature_key == key), None)
    if item is None or item.quality.coverage < 0.999 or item.quality.flags:
        return None
    return item.value


def _driver_feature(driver, key):
    return _feature(driver.features, key) if driver is not None else None

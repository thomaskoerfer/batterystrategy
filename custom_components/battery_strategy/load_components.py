"""Home Assistant adapter for independently modeled load components."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from .component_config import (
    DEFAULT_DHW_ALLOWED_WINDOWS,
    LoadComponentSpec,
    time_allowed,
)
from .const import (
    CONF_CLIMATE_ENTITIES,
    CONF_COMPONENT_KEY,
    CONF_COMPONENT_POWER_ENTITY,
    CONF_DHW_ALLOWED_WINDOWS,
    CONF_HP_ACTIVITY_ENTITY,
    CONF_HP_CIRCULATION_ENTITY,
    CONF_HP_DHW_CHARGING_ENTITY,
    CONF_HP_DHW_DIFFERENTIAL_ENTITY,
    CONF_HP_DHW_TARGET_ENTITY,
    CONF_HP_DHW_TEMP_ENTITY,
    CONF_HP_HEATING_ACTIVE_ENTITY,
    CONF_HP_OUTDOOR_TEMP_ENTITY,
    CONF_HP_TARGET_FLOW_TEMP_ENTITY,
    CONF_LOAD_COMPONENT_PROFILE,
    LOAD_PROFILE_AIR_CONDITIONING,
    LOAD_PROFILE_GENERIC,
    LOAD_PROFILE_HEAT_PUMP,
    SUBENTRY_TYPE_LOAD_COMPONENT,
)
from .contracts import (
    DataQuality,
    LoadDriverSnapshot,
    LoadFeatureValue,
    QualityFlag,
    WeatherSlot,
)


@dataclass(frozen=True, slots=True)
class LoadComponentCollection:
    """Normalized observations and current context for one coordinator tick."""

    powers_w: tuple[tuple[str, float], ...] = ()
    features: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()
    drivers: tuple[LoadDriverSnapshot, ...] = ()
    specs: tuple[LoadComponentSpec, ...] = ()


def add_central_weather(
    collection: LoadComponentCollection, weather: WeatherSlot | None
) -> LoadComponentCollection:
    """Attach one central OAT observation without changing component meters."""
    if weather is None or weather.temperature_c is None:
        return collection
    feature = ("outdoor_temperature_c", weather.temperature_c)
    raw_features = tuple(
        (
            key,
            values
            if any(item[0] == feature[0] for item in values)
            else (*values, feature),
        )
        for key, values in collection.features
    )
    drivers = tuple(
        driver
        if any(item.feature_key == feature[0] for item in driver.features)
        else LoadDriverSnapshot(
            driver.driver_key,
            driver.power_w,
            driver.quality,
            (*driver.features, LoadFeatureValue(*feature)),
        )
        for driver in collection.drivers
    )
    return LoadComponentCollection(
        collection.powers_w, raw_features, drivers, collection.specs
    )


def collect_load_components(hass, entry, now: dt.datetime) -> LoadComponentCollection:
    """Read configured component states without changing whole-house semantics."""
    powers: list[tuple[str, float]] = []
    features: list[tuple[str, tuple[tuple[str, float], ...]]] = []
    drivers: list[LoadDriverSnapshot] = []
    specs: list[LoadComponentSpec] = []
    for subentry in getattr(entry, "subentries", {}).values():
        if getattr(subentry, "subentry_type", None) != SUBENTRY_TYPE_LOAD_COMPONENT:
            continue
        data = dict(subentry.data)
        profile = str(data.get(CONF_LOAD_COMPONENT_PROFILE, ""))
        if profile == LOAD_PROFILE_HEAT_PUMP:
            _collect_heat_pump(hass, data, now, powers, features, drivers, specs)
        elif profile == LOAD_PROFILE_AIR_CONDITIONING:
            _collect_air_conditioning(hass, data, powers, features, drivers, specs)
        elif profile == LOAD_PROFILE_GENERIC:
            _collect_generic(hass, data, powers, features, drivers, specs)
    return LoadComponentCollection(
        tuple(powers), tuple(features), tuple(drivers), tuple(specs)
    )


def _collect_heat_pump(hass, data, now, powers, features, drivers, specs) -> None:
    allowed = str(data.get(CONF_DHW_ALLOWED_WINDOWS, DEFAULT_DHW_ALLOWED_WINDOWS))
    specs.extend(
        (
            LoadComponentSpec("heat_pump_dhw", LOAD_PROFILE_HEAT_PUMP, allowed),
            LoadComponentSpec("heat_pump_space_heating", LOAD_PROFILE_HEAT_PUMP),
        )
    )
    power = _power_w(hass, data.get(CONF_COMPONENT_POWER_ENTITY))
    activity = _state_text(hass, data.get(CONF_HP_ACTIVITY_ENTITY))
    if power is None or activity is None:
        quality = DataQuality(0.0, (QualityFlag.MISSING_COMPONENT,))
        drivers.extend(
            (
                LoadDriverSnapshot("heat_pump_dhw", 0.0, quality),
                LoadDriverSnapshot("heat_pump_space_heating", 0.0, quality),
            )
        )
        return

    activity = activity.lower()
    dhw_power = power if "hot water" in activity or "dhw" in activity else 0.0
    heating_power = power if "heating" in activity else 0.0
    oat = _numeric(hass, data.get(CONF_HP_OUTDOOR_TEMP_ENTITY))
    dhw_temp = _numeric(hass, data.get(CONF_HP_DHW_TEMP_ENTITY))
    dhw_target = _numeric(hass, data.get(CONF_HP_DHW_TARGET_ENTITY))
    dhw_diff = _numeric(hass, data.get(CONF_HP_DHW_DIFFERENTIAL_ENTITY))
    shared = _available_features(("outdoor_temperature_c", oat))
    dhw_features = shared + _available_features(
        ("dhw_temperature_c", dhw_temp),
        ("dhw_target_c", dhw_target),
        ("dhw_differential_c", dhw_diff),
        ("dhw_allowed_fraction", 1.0 if time_allowed(now, allowed) else 0.0),
        ("dhw_charging_fraction", _binary(hass, data.get(CONF_HP_DHW_CHARGING_ENTITY))),
        ("circulation_fraction", _binary(hass, data.get(CONF_HP_CIRCULATION_ENTITY))),
    )
    heating_features = shared + _available_features(
        (
            "heating_active_fraction",
            _binary(hass, data.get(CONF_HP_HEATING_ACTIVE_ENTITY)),
        ),
        (
            "target_flow_temperature_c",
            _numeric(hass, data.get(CONF_HP_TARGET_FLOW_TEMP_ENTITY)),
        ),
    )
    _append_component(
        "heat_pump_dhw", dhw_power, dhw_features, powers, features, drivers
    )
    _append_component(
        "heat_pump_space_heating",
        heating_power,
        heating_features,
        powers,
        features,
        drivers,
    )


def _collect_air_conditioning(hass, data, powers, features, drivers, specs) -> None:
    key = str(data.get(CONF_COMPONENT_KEY) or "air_conditioning")
    specs.append(LoadComponentSpec(key, LOAD_PROFILE_AIR_CONDITIONING))
    power = _power_w(hass, data.get(CONF_COMPONENT_POWER_ENTITY))
    if power is None:
        drivers.append(
            LoadDriverSnapshot(
                key, 0.0, DataQuality(0.0, (QualityFlag.MISSING_COMPONENT,))
            )
        )
        return
    temperatures: list[float] = []
    targets: list[float] = []
    active = 0
    climate_entities = data.get(CONF_CLIMATE_ENTITIES) or []
    if isinstance(climate_entities, str):
        climate_entities = [climate_entities]
    for entity_id in climate_entities:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        current = _finite(state.attributes.get("current_temperature"))
        target = _finite(state.attributes.get("temperature"))
        if current is not None:
            temperatures.append(current)
        if target is not None:
            targets.append(target)
        action = str(state.attributes.get("hvac_action") or "").lower()
        if state.state not in ("off", "unknown", "unavailable") and action != "idle":
            active += 1
    component_features = _available_features(
        ("indoor_temperature_mean_c", _mean(temperatures)),
        ("indoor_temperature_max_c", max(temperatures) if temperatures else None),
        ("target_temperature_mean_c", _mean(targets)),
        ("active_unit_count", float(active)),
    )
    _append_component(key, power, component_features, powers, features, drivers)


def _collect_generic(hass, data, powers, features, drivers, specs) -> None:
    key = str(data.get(CONF_COMPONENT_KEY) or "metered_load")
    specs.append(LoadComponentSpec(key, LOAD_PROFILE_GENERIC))
    power = _power_w(hass, data.get(CONF_COMPONENT_POWER_ENTITY))
    if power is None:
        drivers.append(
            LoadDriverSnapshot(
                key, 0.0, DataQuality(0.0, (QualityFlag.MISSING_COMPONENT,))
            )
        )
        return
    _append_component(key, power, (), powers, features, drivers)


def _append_component(key, power, component_features, powers, features, drivers):
    powers.append((key, power))
    raw_features = tuple((item.feature_key, item.value) for item in component_features)
    features.append((key, raw_features))
    drivers.append(LoadDriverSnapshot(key, power, features=component_features))


def _available_features(*items) -> tuple[LoadFeatureValue, ...]:
    return tuple(
        LoadFeatureValue(key, value) for key, value in items if value is not None
    )


def _power_w(hass, entity_id) -> float | None:
    state = hass.states.get(entity_id) if entity_id else None
    value = _finite(state.state) if state is not None else None
    if value is None or value < 0:
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "W").strip().lower()
    return value * {"kw": 1000.0, "mw": 1_000_000.0}.get(unit, 1.0)


def _numeric(hass, entity_id) -> float | None:
    state = hass.states.get(entity_id) if entity_id else None
    return _finite(state.state) if state is not None else None


def _binary(hass, entity_id) -> float | None:
    state = _state_text(hass, entity_id)
    if state is None:
        return None
    return 1.0 if state.lower() in ("on", "true", "yes", "active", "heating") else 0.0


def _state_text(hass, entity_id) -> str | None:
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or state.state in ("unknown", "unavailable", "none", ""):
        return None
    return str(state.state)


def _finite(value) -> float | None:
    try:
        result = float(value)
    except TypeError, ValueError:
        return None
    return result if math.isfinite(result) else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None

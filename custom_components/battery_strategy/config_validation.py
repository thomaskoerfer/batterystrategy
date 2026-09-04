"""Profile-aware validation for Battery Strategy configuration."""

from __future__ import annotations

from .component_config import validate_allowed_windows
from .const import (
    BATTERY_PROFILE_ZENDURE,
    CONF_BATTERY_INPUT_ENERGY_ENTITY,
    CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_PROFILE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CLIMATE_ENTITIES,
    CONF_COMPONENT_KEY,
    CONF_COMPONENT_POWER_ENTITY,
    CONF_DHW_ALLOWED_WINDOWS,
    CONF_EV_POWER_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_GRID_L1_ENTITY,
    CONF_GRID_L2_ENTITY,
    CONF_GRID_L3_ENTITY,
    CONF_GRID_MODE,
    CONF_HP_ACTIVITY_ENTITY,
    CONF_HP_DHW_DIFFERENTIAL_ENTITY,
    CONF_HP_DHW_TARGET_ENTITY,
    CONF_HP_DHW_TEMP_ENTITY,
    CONF_HP_OUTDOOR_TEMP_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SIGNED_GRID_POWER_ENTITY,
    CONF_ZENDURE_AC_MODE_ENTITY,
    CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
    CONF_ZENDURE_INPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
    CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY,
    CONF_ZENDURE_PACK_INPUT_POWER_ENTITY,
    GRID_MODE_IMPORT_EXPORT,
    GRID_MODE_SIGNED,
    GRID_MODE_THREE_PHASE,
    LOAD_PROFILE_AIR_CONDITIONING,
    LOAD_PROFILE_HEAT_PUMP,
)

OPTIONAL_ENTITY_KEYS = (
    CONF_SIGNED_GRID_POWER_ENTITY,
    CONF_GRID_L1_ENTITY,
    CONF_GRID_L2_ENTITY,
    CONF_GRID_L3_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_BATTERY_INPUT_ENERGY_ENTITY,
    CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_ZENDURE_AC_MODE_ENTITY,
    CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY,
    CONF_ZENDURE_PACK_INPUT_POWER_ENTITY,
    CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
    CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
    CONF_ZENDURE_INPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
)


def default_entry_data() -> dict[str, str]:
    """Return safe, portable defaults for a new installation."""
    return {
        CONF_GRID_MODE: GRID_MODE_THREE_PHASE,
        CONF_BATTERY_PROFILE: BATTERY_PROFILE_ZENDURE,
        CONF_GRID_L1_ENTITY: "",
        CONF_GRID_L2_ENTITY: "",
        CONF_GRID_L3_ENTITY: "",
        CONF_PV_POWER_ENTITY: "",
        CONF_PRICE_ENTITY: "",
        CONF_BATTERY_SOC_ENTITY: "",
        CONF_BATTERY_INPUT_ENERGY_ENTITY: "",
        CONF_BATTERY_OUTPUT_ENERGY_ENTITY: "",
        CONF_EV_POWER_ENTITY: "",
        CONF_ZENDURE_AC_MODE_ENTITY: "",
        CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY: "",
        CONF_ZENDURE_PACK_INPUT_POWER_ENTITY: "",
        CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY: "",
        CONF_ZENDURE_GRID_INPUT_POWER_ENTITY: "",
        CONF_ZENDURE_INPUT_LIMIT_ENTITY: "",
        CONF_ZENDURE_OUTPUT_LIMIT_ENTITY: "",
    }


def required_entity_keys(data: dict[str, str]) -> set[str]:
    """Return entity mappings required by the selected source profiles."""
    required = {
        CONF_PV_POWER_ENTITY,
        CONF_PRICE_ENTITY,
        CONF_BATTERY_SOC_ENTITY,
    }
    grid_mode = data.get(CONF_GRID_MODE, GRID_MODE_THREE_PHASE)
    if grid_mode == GRID_MODE_SIGNED:
        required.add(CONF_SIGNED_GRID_POWER_ENTITY)
    elif grid_mode == GRID_MODE_IMPORT_EXPORT:
        required.update((CONF_GRID_IMPORT_ENTITY, CONF_GRID_EXPORT_ENTITY))
    else:
        required.update((CONF_GRID_L1_ENTITY, CONF_GRID_L2_ENTITY, CONF_GRID_L3_ENTITY))

    if (
        data.get(CONF_BATTERY_PROFILE, BATTERY_PROFILE_ZENDURE)
        == BATTERY_PROFILE_ZENDURE
    ):
        required.update(
            (
                CONF_ZENDURE_AC_MODE_ENTITY,
                CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
                CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
                CONF_ZENDURE_INPUT_LIMIT_ENTITY,
                CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
            )
        )
    else:
        required.add(CONF_BATTERY_POWER_ENTITY)
    return required


def validate_entity_mapping(hass, data: dict[str, str]) -> dict[str, str]:
    """Validate profile completeness and measurement units before saving."""
    errors = {
        key: "required" for key in required_entity_keys(data) if not data.get(key)
    }
    power_keys = {
        CONF_SIGNED_GRID_POWER_ENTITY,
        CONF_GRID_L1_ENTITY,
        CONF_GRID_L2_ENTITY,
        CONF_GRID_L3_ENTITY,
        CONF_GRID_IMPORT_ENTITY,
        CONF_GRID_EXPORT_ENTITY,
        CONF_PV_POWER_ENTITY,
        CONF_EV_POWER_ENTITY,
        CONF_BATTERY_POWER_ENTITY,
        CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY,
        CONF_ZENDURE_PACK_INPUT_POWER_ENTITY,
        CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
        CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
    }
    energy_keys = {CONF_BATTERY_INPUT_ENERGY_ENTITY, CONF_BATTERY_OUTPUT_ENERGY_ENTITY}
    entity_keys = (
        power_keys
        | energy_keys
        | {
            CONF_PRICE_ENTITY,
            CONF_BATTERY_SOC_ENTITY,
            CONF_ZENDURE_AC_MODE_ENTITY,
            CONF_ZENDURE_INPUT_LIMIT_ENTITY,
            CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
        }
    )
    for key, entity_id in data.items():
        if key not in entity_keys or not entity_id or key in errors:
            continue
        state = hass.states.get(entity_id)
        if state is None:
            errors[key] = "entity_not_found"
            continue
        unit = str(state.attributes.get("unit_of_measurement") or "").strip().lower()
        if key in power_keys and unit not in {"w", "kw", "mw"}:
            errors[key] = "invalid_power_unit"
        elif key in energy_keys and unit != "kwh":
            errors[key] = "invalid_energy_unit"
        elif key == CONF_BATTERY_SOC_ENTITY and unit != "%":
            errors[key] = "invalid_soc_unit"

    price_entity = data.get(CONF_PRICE_ENTITY)
    price_state = hass.states.get(price_entity) if price_entity else None
    if price_state is not None and not isinstance(
        price_state.attributes.get("data"), list
    ):
        errors[CONF_PRICE_ENTITY] = "invalid_price_entity"
    return errors


def validate_load_component(hass, profile: str, data: dict) -> dict[str, str]:
    """Reject missing meters and malformed semantic keys before collection."""
    errors: dict[str, str] = {}
    power_entity = data.get(CONF_COMPONENT_POWER_ENTITY)
    state = hass.states.get(power_entity) if power_entity else None
    if state is None:
        errors[CONF_COMPONENT_POWER_ENTITY] = "entity_not_found"
    elif str(state.attributes.get("unit_of_measurement") or "").lower() not in {
        "w",
        "kw",
        "mw",
    }:
        errors[CONF_COMPONENT_POWER_ENTITY] = "invalid_power_unit"
    if profile == LOAD_PROFILE_HEAT_PUMP and not validate_allowed_windows(
        str(data.get(CONF_DHW_ALLOWED_WINDOWS, ""))
    ):
        errors[CONF_DHW_ALLOWED_WINDOWS] = "invalid_time_windows"
    if profile == LOAD_PROFILE_HEAT_PUMP:
        for key in (
            CONF_HP_ACTIVITY_ENTITY,
            CONF_HP_OUTDOOR_TEMP_ENTITY,
            CONF_HP_DHW_TEMP_ENTITY,
            CONF_HP_DHW_TARGET_ENTITY,
            CONF_HP_DHW_DIFFERENTIAL_ENTITY,
        ):
            entity_id = data.get(key)
            if not entity_id or hass.states.get(entity_id) is None:
                errors[key] = "entity_not_found"
    if profile == LOAD_PROFILE_AIR_CONDITIONING:
        climates = data.get(CONF_CLIMATE_ENTITIES) or []
        if isinstance(climates, str):
            climates = [climates]
        if not climates:
            errors[CONF_CLIMATE_ENTITIES] = "required"
        elif any(hass.states.get(entity_id) is None for entity_id in climates):
            errors[CONF_CLIMATE_ENTITIES] = "entity_not_found"
    key = str(data.get(CONF_COMPONENT_KEY, ""))
    if profile != LOAD_PROFILE_HEAT_PUMP and (
        not key
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in key
        )
    ):
        errors[CONF_COMPONENT_KEY] = "invalid_component_key"
    return errors


def load_component_unique_id(profile: str, data: dict) -> str:
    """Return stable subentry identity independent of its display name."""
    key = (
        "heat_pump"
        if profile == LOAD_PROFILE_HEAT_PUMP
        else str(data.get(CONF_COMPONENT_KEY, "metered_load"))
    )
    return f"{profile}:{key}"

from __future__ import annotations

from types import SimpleNamespace

from custom_components.battery_strategy.config_validation import (
    duplicate_load_component_key,
    validate_load_component,
)
from custom_components.battery_strategy.const import (
    CONF_APPLIANCE_ACTIVITY_ENTITY,
    CONF_APPLIANCE_END_TIME_ENTITY,
    CONF_APPLIANCE_PROGRESS_ENTITY,
    CONF_COMPONENT_KEY,
    CONF_COMPONENT_POWER_ENTITY,
    LOAD_PROFILE_CYCLIC_APPLIANCE,
)


class _States:
    def __init__(self, values):
        self._values = values

    def get(self, entity_id):
        return self._values.get(entity_id)


def test_cyclic_appliance_optional_entities_are_validated_when_configured():
    power = SimpleNamespace(attributes={"unit_of_measurement": "W"})
    hass = SimpleNamespace(states=_States({"sensor.power": power}))

    errors = validate_load_component(
        hass,
        LOAD_PROFILE_CYCLIC_APPLIANCE,
        {
            CONF_COMPONENT_KEY: "washer",
            CONF_COMPONENT_POWER_ENTITY: "sensor.power",
            CONF_APPLIANCE_ACTIVITY_ENTITY: "sensor.activity",
            CONF_APPLIANCE_PROGRESS_ENTITY: "sensor.progress",
            CONF_APPLIANCE_END_TIME_ENTITY: "sensor.end_time",
        },
    )

    assert errors == {
        CONF_APPLIANCE_ACTIVITY_ENTITY: "entity_not_found",
        CONF_APPLIANCE_PROGRESS_ENTITY: "entity_not_found",
        CONF_APPLIANCE_END_TIME_ENTITY: "entity_not_found",
    }


def test_cyclic_appliance_context_entities_remain_optional():
    power = SimpleNamespace(attributes={"unit_of_measurement": "W"})
    hass = SimpleNamespace(states=_States({"sensor.power": power}))

    errors = validate_load_component(
        hass,
        LOAD_PROFILE_CYCLIC_APPLIANCE,
        {
            CONF_COMPONENT_KEY: "dryer",
            CONF_COMPONENT_POWER_ENTITY: "sensor.power",
        },
    )

    assert errors == {}


def test_component_keys_are_unique_across_profiles():
    entry = SimpleNamespace(
        subentries={
            "generic": SimpleNamespace(
                data={
                    "profile": "generic_metered",
                    CONF_COMPONENT_KEY: "dryer",
                }
            )
        }
    )

    assert duplicate_load_component_key(
        entry,
        LOAD_PROFILE_CYCLIC_APPLIANCE,
        {CONF_COMPONENT_KEY: "dryer"},
    )


def test_heat_pump_runtime_keys_are_reserved_for_other_profiles():
    entry = SimpleNamespace(
        subentries={"heat_pump": SimpleNamespace(data={"profile": "ems_esp_heat_pump"})}
    )

    assert duplicate_load_component_key(
        entry,
        LOAD_PROFILE_CYCLIC_APPLIANCE,
        {CONF_COMPONENT_KEY: "heat_pump_dhw"},
    )


def test_general_house_load_key_is_reserved():
    assert duplicate_load_component_key(
        SimpleNamespace(subentries={}),
        LOAD_PROFILE_CYCLIC_APPLIANCE,
        {CONF_COMPONENT_KEY: "general_house_load"},
    )


def test_heat_pump_keys_are_reserved_without_an_existing_heat_pump():
    assert duplicate_load_component_key(
        SimpleNamespace(subentries={}),
        LOAD_PROFILE_CYCLIC_APPLIANCE,
        {CONF_COMPONENT_KEY: "heat_pump_dhw"},
    )

"""Tests for independent Home Assistant load-component adapters."""

from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace

from custom_components.battery_strategy.const import (
    CONF_APPLIANCE_ACTIVITY_ENTITY,
    CONF_APPLIANCE_END_TIME_ENTITY,
    CONF_APPLIANCE_PROGRESS_ENTITY,
    CONF_CLIMATE_ENTITIES,
    CONF_COMPONENT_KEY,
    CONF_COMPONENT_POWER_ENTITY,
    CONF_DHW_ALLOWED_WINDOWS,
    CONF_HP_ACTIVITY_ENTITY,
    CONF_HP_DHW_CHARGING_ENTITY,
    CONF_HP_DHW_DIFFERENTIAL_ENTITY,
    CONF_HP_DHW_TARGET_ENTITY,
    CONF_HP_DHW_TEMP_ENTITY,
    CONF_HP_OUTDOOR_TEMP_ENTITY,
    CONF_LOAD_COMPONENT_PROFILE,
    LOAD_PROFILE_AIR_CONDITIONING,
    LOAD_PROFILE_CYCLIC_APPLIANCE,
    LOAD_PROFILE_GENERIC,
    LOAD_PROFILE_HEAT_PUMP,
    SUBENTRY_TYPE_LOAD_COMPONENT,
)
from custom_components.battery_strategy.contracts import SlotKey, WeatherSlot
from custom_components.battery_strategy.load_components import (
    add_central_weather,
    collect_load_components,
)


class _States:
    def __init__(self, values):
        self._values = values

    def get(self, entity_id):
        return self._values.get(entity_id)


def _state(value, unit=None, last_changed=None, **attributes):
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return SimpleNamespace(
        state=str(value), attributes=attributes, last_changed=last_changed
    )


class LoadComponentAdapterTests(unittest.TestCase):
    def test_runtime_ignores_legacy_duplicate_component_keys(self):
        entry = SimpleNamespace(
            subentries={
                "first": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT,
                    data={
                        CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_CYCLIC_APPLIANCE,
                        CONF_COMPONENT_KEY: "appliance",
                        CONF_COMPONENT_POWER_ENTITY: "sensor.first",
                    },
                ),
                "second": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT,
                    data={
                        CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_GENERIC,
                        CONF_COMPONENT_KEY: "appliance",
                        CONF_COMPONENT_POWER_ENTITY: "sensor.second",
                    },
                ),
            }
        )
        hass = SimpleNamespace(
            states=_States(
                {
                    "sensor.first": _state(100, "W"),
                    "sensor.second": _state(200, "W"),
                }
            )
        )

        result = collect_load_components(hass, entry, dt.datetime.now(dt.UTC))

        self.assertEqual(result.powers_w, (("appliance", 100.0),))
        self.assertEqual(result.ignored_duplicate_keys, ("appliance",))

    def test_cyclic_appliance_exposes_optional_cycle_context(self):
        now = dt.datetime(2026, 9, 8, 12, 30, tzinfo=dt.UTC)
        data = {
            CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_CYCLIC_APPLIANCE,
            CONF_COMPONENT_POWER_ENTITY: "sensor.appliance_power",
            "component_key": "washing_machine",
            CONF_APPLIANCE_ACTIVITY_ENTITY: "sensor.appliance_state",
            CONF_APPLIANCE_PROGRESS_ENTITY: "sensor.appliance_progress",
            CONF_APPLIANCE_END_TIME_ENTITY: "sensor.appliance_end",
        }
        entry = SimpleNamespace(
            subentries={
                "appliance": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT, data=data
                )
            }
        )
        hass = SimpleNamespace(
            states=_States(
                {
                    "sensor.appliance_power": _state(0.65, "kW"),
                    "sensor.appliance_state": _state(
                        "run", last_changed=now - dt.timedelta(minutes=20)
                    ),
                    "sensor.appliance_progress": _state(25, "%"),
                    "sensor.appliance_end": _state("2026-09-08T13:40:00+00:00"),
                }
            )
        )

        result = collect_load_components(hass, entry, now)
        driver = result.drivers[0]
        features = {item.feature_key: item.value for item in driver.features}

        self.assertEqual(dict(result.powers_w)["washing_machine"], 650.0)
        self.assertEqual(features["cycle_active_fraction"], 1.0)
        self.assertEqual(features["cycle_active_age_s"], 1200.0)
        self.assertEqual(features["cycle_progress_fraction"], 0.25)
        self.assertEqual(features["cycle_remaining_s"], 4200.0)

    def test_heat_pump_exposes_active_dhw_state_age(self):
        now = dt.datetime(2026, 9, 7, 3, 0, tzinfo=dt.UTC)
        data = {
            CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_HEAT_PUMP,
            CONF_COMPONENT_POWER_ENTITY: "sensor.hp_power",
            CONF_HP_ACTIVITY_ENTITY: "sensor.hp_activity",
            CONF_HP_DHW_CHARGING_ENTITY: "binary_sensor.dhw_charging",
        }
        entry = SimpleNamespace(
            subentries={
                "hp": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT, data=data
                )
            }
        )
        hass = SimpleNamespace(
            states=_States(
                {
                    "sensor.hp_power": _state(1800, "W"),
                    "sensor.hp_activity": _state("hot water"),
                    "binary_sensor.dhw_charging": _state(
                        "on", last_changed=now - dt.timedelta(seconds=95)
                    ),
                }
            )
        )

        result = collect_load_components(hass, entry, now)
        features = {item.feature_key: item.value for item in result.drivers[0].features}

        self.assertEqual(features["dhw_charging_fraction"], 1.0)
        self.assertEqual(features["dhw_active_age_s"], 95.0)

    def test_heat_pump_uses_required_activity_age_without_optional_binary(self):
        now = dt.datetime(2026, 9, 7, 3, 0, tzinfo=dt.UTC)
        data = {
            CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_HEAT_PUMP,
            CONF_COMPONENT_POWER_ENTITY: "sensor.hp_power",
            CONF_HP_ACTIVITY_ENTITY: "sensor.hp_activity",
        }
        entry = SimpleNamespace(
            subentries={
                "hp": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT, data=data
                )
            }
        )
        hass = SimpleNamespace(
            states=_States(
                {
                    "sensor.hp_power": _state(1800, "W"),
                    "sensor.hp_activity": _state(
                        "hot water", last_changed=now - dt.timedelta(seconds=125)
                    ),
                }
            )
        )

        result = collect_load_components(hass, entry, now)
        features = {item.feature_key: item.value for item in result.drivers[0].features}

        self.assertEqual(features["dhw_active_age_s"], 125.0)

    def test_heat_pump_power_is_split_by_activity(self):
        data = {
            CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_HEAT_PUMP,
            CONF_COMPONENT_POWER_ENTITY: "sensor.hp_power",
            CONF_HP_ACTIVITY_ENTITY: "sensor.hp_activity",
            CONF_HP_OUTDOOR_TEMP_ENTITY: "sensor.oat",
            CONF_HP_DHW_TEMP_ENTITY: "sensor.dhw",
            CONF_HP_DHW_TARGET_ENTITY: "sensor.target",
            CONF_HP_DHW_DIFFERENTIAL_ENTITY: "number.diff",
            CONF_DHW_ALLOWED_WINDOWS: "00:00-05:00,09:00-17:00",
        }
        entry = SimpleNamespace(
            subentries={
                "hp": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT, data=data
                )
            }
        )
        hass = SimpleNamespace(
            states=_States(
                {
                    "sensor.hp_power": _state(2.5, "kW"),
                    "sensor.hp_activity": _state("hot water"),
                    "sensor.oat": _state(12.0),
                    "sensor.dhw": _state(44.0),
                    "sensor.target": _state(53.0),
                    "number.diff": _state(9.0),
                }
            )
        )
        result = collect_load_components(
            hass, entry, dt.datetime(2026, 8, 22, 10, tzinfo=dt.UTC)
        )
        self.assertEqual(dict(result.powers_w)["heat_pump_dhw"], 2500.0)
        self.assertEqual(dict(result.powers_w)["heat_pump_space_heating"], 0.0)

    def test_heat_pump_temperatures_are_normalized_to_celsius(self):
        data = {
            CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_HEAT_PUMP,
            CONF_COMPONENT_POWER_ENTITY: "sensor.hp_power",
            CONF_HP_ACTIVITY_ENTITY: "sensor.hp_activity",
            CONF_HP_OUTDOOR_TEMP_ENTITY: "sensor.oat",
            CONF_HP_DHW_TEMP_ENTITY: "sensor.dhw",
            CONF_HP_DHW_TARGET_ENTITY: "sensor.target",
            CONF_HP_DHW_DIFFERENTIAL_ENTITY: "number.diff",
            CONF_DHW_ALLOWED_WINDOWS: "00:00-05:00,09:00-17:00",
        }
        entry = SimpleNamespace(
            subentries={
                "hp": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT, data=data
                )
            }
        )
        hass = SimpleNamespace(
            states=_States(
                {
                    "sensor.hp_power": _state(2500, "W"),
                    "sensor.hp_activity": _state("hot water"),
                    "sensor.oat": _state(50.0, "°F"),
                    "sensor.dhw": _state(111.2, "°F"),
                    "sensor.target": _state(127.4, "°F"),
                    "number.diff": _state(16.2, "°F"),
                }
            )
        )

        result = collect_load_components(hass, entry, dt.datetime.now(dt.UTC))
        features = {item.feature_key: item.value for item in result.drivers[0].features}

        self.assertAlmostEqual(features["outdoor_temperature_c"], 10.0)
        self.assertAlmostEqual(features["dhw_temperature_c"], 44.0)
        self.assertAlmostEqual(features["dhw_target_c"], 53.0)
        self.assertAlmostEqual(features["dhw_differential_c"], 9.0)

    def test_heat_pump_kelvin_absolute_and_differential_values_are_distinct(self):
        data = {
            CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_HEAT_PUMP,
            CONF_COMPONENT_POWER_ENTITY: "sensor.hp_power",
            CONF_HP_ACTIVITY_ENTITY: "sensor.hp_activity",
            CONF_HP_OUTDOOR_TEMP_ENTITY: "sensor.oat",
            CONF_HP_DHW_TEMP_ENTITY: "sensor.dhw",
            CONF_HP_DHW_TARGET_ENTITY: "sensor.target",
            CONF_HP_DHW_DIFFERENTIAL_ENTITY: "number.diff",
            CONF_DHW_ALLOWED_WINDOWS: "00:00-05:00,09:00-17:00",
        }
        entry = SimpleNamespace(
            subentries={
                "hp": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT, data=data
                )
            }
        )
        hass = SimpleNamespace(
            states=_States(
                {
                    "sensor.hp_power": _state(2500, "W"),
                    "sensor.hp_activity": _state("hot water"),
                    "sensor.oat": _state(283.15, "K"),
                    "sensor.dhw": _state(317.15, "K"),
                    "sensor.target": _state(326.15, "K"),
                    "number.diff": _state(9.0, "K"),
                }
            )
        )

        result = collect_load_components(hass, entry, dt.datetime.now(dt.UTC))
        features = {item.feature_key: item.value for item in result.drivers[0].features}

        self.assertAlmostEqual(features["outdoor_temperature_c"], 10.0)
        self.assertAlmostEqual(features["dhw_temperature_c"], 44.0)
        self.assertAlmostEqual(features["dhw_target_c"], 53.0)
        self.assertAlmostEqual(features["dhw_differential_c"], 9.0)

    def test_air_conditioning_uses_common_meter_once_for_four_rooms(self):
        climates = [f"climate.room_{index}" for index in range(4)]
        data = {
            CONF_LOAD_COMPONENT_PROFILE: LOAD_PROFILE_AIR_CONDITIONING,
            CONF_COMPONENT_POWER_ENTITY: "sensor.ac_power",
            CONF_CLIMATE_ENTITIES: climates,
        }
        entry = SimpleNamespace(
            subentries={
                "ac": SimpleNamespace(
                    subentry_type=SUBENTRY_TYPE_LOAD_COMPONENT, data=data
                )
            }
        )
        states = {"sensor.ac_power": _state(900, "W")}
        states.update(
            {
                entity_id: _state(
                    "cool",
                    current_temperature=25 + index,
                    temperature=22,
                    hvac_action="cooling" if index < 2 else "idle",
                )
                for index, entity_id in enumerate(climates)
            }
        )
        result = collect_load_components(
            SimpleNamespace(states=_States(states)),
            entry,
            dt.datetime.now(dt.UTC),
        )
        self.assertEqual(dict(result.powers_w)["air_conditioning"], 900.0)
        driver = result.drivers[0]
        features = {item.feature_key: item.value for item in driver.features}
        self.assertEqual(features["active_unit_count"], 2.0)
        self.assertEqual(features["indoor_temperature_max_c"], 28.0)
        enriched = add_central_weather(
            result, WeatherSlot(SlotKey(0, 900_000), temperature_c=31.0)
        )
        enriched_features = {
            item.feature_key: item.value for item in enriched.drivers[0].features
        }
        self.assertEqual(enriched_features["outdoor_temperature_c"], 31.0)

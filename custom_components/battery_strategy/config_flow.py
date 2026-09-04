"""Config flow for Battery Strategy."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .component_config import DEFAULT_DHW_ALLOWED_WINDOWS
from .config_definitions import numeric_option, option_default
from .config_validation import (
    OPTIONAL_ENTITY_KEYS,
)
from .config_validation import (
    default_entry_data as _default_entry_data,
)
from .config_validation import (
    load_component_unique_id as _load_component_unique_id,
)
from .config_validation import (
    validate_entity_mapping as _validate_entity_mapping,
)
from .config_validation import (
    validate_load_component as _validate_load_component,
)
from .const import (
    BATTERY_PROFILE_GENERIC,
    BATTERY_PROFILE_ZENDURE,
    CONF_BATTERY_CAPACITY_KWH,
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
    CONF_HP_CIRCULATION_ENTITY,
    CONF_HP_DHW_CHARGING_ENTITY,
    CONF_HP_DHW_DIFFERENTIAL_ENTITY,
    CONF_HP_DHW_TARGET_ENTITY,
    CONF_HP_DHW_TEMP_ENTITY,
    CONF_HP_HEATING_ACTIVE_ENTITY,
    CONF_HP_OUTDOOR_TEMP_ENTITY,
    CONF_HP_TARGET_FLOW_TEMP_ENTITY,
    CONF_LOAD_COMPONENT_NAME,
    CONF_LOAD_COMPONENT_PROFILE,
    CONF_PRICE_ENTITY,
    CONF_PV_CAPACITY_KWP,
    CONF_PV_INVERTER_POWER_KW,
    CONF_PV_POWER_ENTITY,
    CONF_SIGNED_GRID_POWER_ENTITY,
    CONF_ZENDURE_AC_MODE_ENTITY,
    CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
    CONF_ZENDURE_INPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
    CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY,
    CONF_ZENDURE_PACK_INPUT_POWER_ENTITY,
    CONFIG_ENTRY_VERSION,
    DISCHARGE_LOAD,
    DISCHARGE_OFF,
    DISCHARGE_PRICE_SENSITIVE,
    DOMAIN,
    GRID_CHARGING_OFF,
    GRID_CHARGING_PRICE_SENSITIVE,
    GRID_MODE_IMPORT_EXPORT,
    GRID_MODE_SIGNED,
    GRID_MODE_THREE_PHASE,
    LOAD_PROFILE_AIR_CONDITIONING,
    LOAD_PROFILE_GENERIC,
    LOAD_PROFILE_HEAT_PUMP,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    MANUAL_OFF,
    PV_CHARGING_OFF,
    PV_CHARGING_ON,
    SUBENTRY_TYPE_LOAD_COMPONENT,
)


def _entity_selector(domains: list[str] | None = None):
    """Return a Home Assistant entity selector."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains))


def _multiple_entity_selector(domains: list[str]):
    """Return a multiple Home Assistant entity selector."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domains, multiple=True)
    )


def _select_selector(options: list[str]):
    """Return a select selector."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def _translated_select_selector(options: list[str], translation_key: str):
    """Return a select whose labels come from integration translations."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=translation_key,
        )
    )


def _select_label_selector(options: list[dict[str, str]]):
    """Return a select selector with explicit frontend labels."""
    if selector is None:
        return vol.In([item["value"] for item in options])
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def _number_selector(
    minimum: float,
    maximum: float,
    step: float,
    unit: str | None = None,
    mode: str = "box",
):
    """Return a number selector."""
    if selector is None:
        return vol.Coerce(float)
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=mode
        )
    )


def _number_option_selector(key: str):
    """Return the selector from the canonical numeric option definition."""
    definition = numeric_option(key)
    return _number_selector(
        definition.config_minimum,
        definition.config_maximum,
        definition.config_step,
        definition.unit,
    )


def _number_option_default(options: dict, key: str) -> float:
    """Return a submitted value or the historic config-flow default."""
    return float(options.get(key, numeric_option(key).config_default))


def _optional_entity_key(key: str, value: str):
    """Return an optional entity field that can also be cleared."""
    description = {"suggested_value": value} if value else None
    return vol.Optional(key, description=description)


class BatteryStrategyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Battery Strategy."""

    VERSION = CONFIG_ENTRY_VERSION

    @classmethod
    def async_get_supported_subentry_types(cls, config_entry):
        """Return independently configurable load-component profiles."""
        return {SUBENTRY_TYPE_LOAD_COMPONENT: LoadComponentSubentryFlowHandler}

    async def async_step_import(self, user_input):
        """Import a minimal YAML configuration."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        data = _default_entry_data()
        data.update(user_input or {})
        return self.async_create_entry(title="Battery Strategy", data=data)

    async def async_step_user(self, user_input=None):
        """Create the integration entry."""
        if user_input is not None:
            errors = _validate_entity_mapping(self.hass, user_input)
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_entity_schema(user_input),
                    errors=errors,
                )
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Battery Strategy", data=user_input)

        schema = _entity_schema(_default_entry_data())
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(self, user_input=None):
        """Update source and control entities with one managed reload."""
        entry = self._get_reconfigure_entry()
        current = dict(_default_entry_data())
        current.update(dict(entry.data))
        if user_input is not None:
            data = dict(current)
            for key in OPTIONAL_ENTITY_KEYS:
                data[key] = user_input.get(key, "")
            data.update(user_input)
            errors = _validate_entity_mapping(self.hass, data)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=data,
                )
            current = data
        else:
            errors = {}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_entity_schema(current),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return BatteryStrategyOptionsFlow()


class LoadComponentSubentryFlowHandler(config_entries.ConfigSubentryFlow):
    """Add or reconfigure one independently metered load profile."""

    def __init__(self) -> None:
        self._profile: str | None = None

    async def async_step_user(self, user_input=None):
        """Select a profile before showing its compact mapping form."""
        if user_input is not None:
            self._profile = str(user_input[CONF_LOAD_COMPONENT_PROFILE])
            return await self.async_step_details()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOAD_COMPONENT_PROFILE
                    ): _translated_select_selector(
                        [
                            LOAD_PROFILE_HEAT_PUMP,
                            LOAD_PROFILE_AIR_CONDITIONING,
                            LOAD_PROFILE_GENERIC,
                        ],
                        "load_component_profile",
                    )
                }
            ),
        )

    async def async_step_details(self, user_input=None):
        """Configure the selected profile."""
        if user_input is not None:
            errors = _validate_load_component(self.hass, self._profile, user_input)
            if not errors:
                data = dict(user_input)
                data[CONF_LOAD_COMPONENT_PROFILE] = self._profile
                return self.async_create_entry(
                    title=str(data.get(CONF_LOAD_COMPONENT_NAME) or self._profile),
                    data=data,
                    unique_id=_load_component_unique_id(self._profile, data),
                )
            defaults = user_input
        else:
            errors = {}
            defaults = {}
        return self.async_show_form(
            step_id="details",
            data_schema=_load_component_schema(self.hass, self._profile, defaults),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Reconfigure one existing load component and reload once."""
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)
        self._profile = str(
            defaults.get(CONF_LOAD_COMPONENT_PROFILE, LOAD_PROFILE_GENERIC)
        )
        if user_input is not None:
            errors = _validate_load_component(self.hass, self._profile, user_input)
            if not errors:
                data = dict(user_input)
                data[CONF_LOAD_COMPONENT_PROFILE] = self._profile
                return self.async_update_reload_and_abort(
                    self._get_entry(),
                    subentry,
                    data=data,
                    title=str(data.get(CONF_LOAD_COMPONENT_NAME) or self._profile),
                    unique_id=_load_component_unique_id(self._profile, data),
                )
            defaults = user_input
        else:
            errors = {}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_load_component_schema(self.hass, self._profile, defaults),
            errors=errors,
        )


class BatteryStrategyOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle Battery Strategy options."""

    async def async_step_init(self, user_input=None):
        """Show strategy options."""
        return await self.async_step_strategy(user_input)

    async def async_step_strategy(self, user_input=None):
        """Show strategy options sections."""
        if user_input is not None:
            section = user_input.get("strategy_section")
            if section == "ev":
                return await self.async_step_strategy_ev()
            if section == "battery":
                return await self.async_step_strategy_battery()
            if section == "manual":
                return await self.async_step_strategy_manual()
            return await self.async_step_strategy_mode()

        schema = vol.Schema(
            {
                vol.Required(
                    "strategy_section", default="mode"
                ): _select_label_selector(
                    [
                        {"value": "mode", "label": "Modus"},
                        {"value": "ev", "label": "EV Policy"},
                        {"value": "battery", "label": "Batterie und Preise"},
                        {"value": "manual", "label": "Manueller Override"},
                    ]
                )
            }
        )
        return self.async_show_form(step_id="strategy", data_schema=schema)

    async def async_step_strategy_mode(self, user_input=None):
        """Manage high-level strategy mode options."""
        if user_input is not None:
            return self._save_options(user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    "strategy_enabled",
                    default=options.get(
                        "strategy_enabled", option_default("strategy_enabled")
                    ),
                ): bool,
                vol.Required(
                    "trace_enabled",
                    default=options.get(
                        "trace_enabled", option_default("trace_enabled")
                    ),
                ): bool,
                vol.Required(
                    "pv_charging",
                    default=options.get("pv_charging", option_default("pv_charging")),
                ): _select_selector([PV_CHARGING_OFF, PV_CHARGING_ON]),
                vol.Required(
                    "grid_charging",
                    default=options.get(
                        "grid_charging", option_default("grid_charging")
                    ),
                ): _select_selector([GRID_CHARGING_OFF, GRID_CHARGING_PRICE_SENSITIVE]),
                vol.Required(
                    "discharge",
                    default=options.get("discharge", option_default("discharge")),
                ): _select_selector(
                    [DISCHARGE_OFF, DISCHARGE_LOAD, DISCHARGE_PRICE_SENSITIVE]
                ),
            }
        )
        return self.async_show_form(step_id="strategy_mode", data_schema=schema)

    async def async_step_strategy_ev(self, user_input=None):
        """Manage EV policy options."""
        if user_input is not None:
            return self._save_options(user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    "pv_to_ev_first",
                    default=options.get(
                        "pv_to_ev_first", option_default("pv_to_ev_first")
                    ),
                ): bool,
                vol.Required(
                    "discharge_during_ev_charging",
                    default=options.get(
                        "discharge_during_ev_charging",
                        option_default("discharge_during_ev_charging"),
                    ),
                ): bool,
                vol.Required(
                    "battery_may_feed_ev",
                    default=options.get(
                        "battery_may_feed_ev", option_default("battery_may_feed_ev")
                    ),
                ): bool,
                vol.Required(
                    "ev_active_threshold_w",
                    default=_number_option_default(options, "ev_active_threshold_w"),
                ): _number_option_selector("ev_active_threshold_w"),
            }
        )
        return self.async_show_form(step_id="strategy_ev", data_schema=schema)

    async def async_step_strategy_battery(self, user_input=None):
        """Manage battery and price options."""
        if user_input is not None:
            errors = {}
            if float(user_input["min_soc_pct"]) >= float(user_input["max_soc_pct"]):
                errors["base"] = "invalid_soc_range"
            if errors:
                return self.async_show_form(
                    step_id="strategy_battery",
                    data_schema=self._battery_schema(user_input),
                    errors=errors,
                )
            return self._save_options(user_input)

        return self.async_show_form(
            step_id="strategy_battery",
            data_schema=self._battery_schema(dict(self.config_entry.options)),
        )

    @staticmethod
    def _battery_schema(options):
        """Return battery options schema with submitted values preserved."""
        return vol.Schema(
            {
                vol.Required(
                    "min_soc_pct",
                    default=_number_option_default(options, "min_soc_pct"),
                ): _number_option_selector("min_soc_pct"),
                vol.Required(
                    "max_soc_pct",
                    default=_number_option_default(options, "max_soc_pct"),
                ): _number_option_selector("max_soc_pct"),
                vol.Required(
                    "max_charge_power_w",
                    default=_number_option_default(options, "max_charge_power_w"),
                ): _number_option_selector("max_charge_power_w"),
                vol.Required(
                    "max_discharge_power_w",
                    default=_number_option_default(options, "max_discharge_power_w"),
                ): _number_option_selector("max_discharge_power_w"),
                vol.Required(
                    "min_command_power_w",
                    default=_number_option_default(options, "min_command_power_w"),
                ): _number_option_selector("min_command_power_w"),
                vol.Required(
                    "min_command_delta_w",
                    default=_number_option_default(options, "min_command_delta_w"),
                ): _number_option_selector("min_command_delta_w"),
                vol.Required(
                    "round_trip_efficiency",
                    default=_number_option_default(options, "round_trip_efficiency"),
                ): _number_option_selector("round_trip_efficiency"),
                vol.Required(
                    "min_margin_ct_per_kwh",
                    default=_number_option_default(options, "min_margin_ct_per_kwh"),
                ): _number_option_selector("min_margin_ct_per_kwh"),
                vol.Required(
                    "planning_horizon_h",
                    default=_number_option_default(options, "planning_horizon_h"),
                ): _number_option_selector("planning_horizon_h"),
                vol.Required(
                    "feed_in_tariff_ct_per_kwh",
                    default=_number_option_default(
                        options, "feed_in_tariff_ct_per_kwh"
                    ),
                ): _number_option_selector("feed_in_tariff_ct_per_kwh"),
                vol.Required(
                    CONF_BATTERY_CAPACITY_KWH,
                    default=_number_option_default(options, CONF_BATTERY_CAPACITY_KWH),
                ): _number_option_selector(CONF_BATTERY_CAPACITY_KWH),
                vol.Required(
                    CONF_PV_CAPACITY_KWP,
                    default=_number_option_default(options, CONF_PV_CAPACITY_KWP),
                ): _number_option_selector(CONF_PV_CAPACITY_KWP),
                vol.Required(
                    CONF_PV_INVERTER_POWER_KW,
                    default=_number_option_default(options, CONF_PV_INVERTER_POWER_KW),
                ): _number_option_selector(CONF_PV_INVERTER_POWER_KW),
            }
        )

    async def async_step_strategy_manual(self, user_input=None):
        """Manage manual override options."""
        if user_input is not None:
            return self._save_options(user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    "manual_mode",
                    default=options.get("manual_mode", option_default("manual_mode")),
                ): _select_selector([MANUAL_OFF, MANUAL_CHARGE, MANUAL_DISCHARGE]),
                vol.Required(
                    "manual_power_w",
                    default=_number_option_default(options, "manual_power_w"),
                ): _number_option_selector("manual_power_w"),
            }
        )
        return self.async_show_form(step_id="strategy_manual", data_schema=schema)

    def _save_options(self, user_input):
        """Persist partial options without dropping other sections."""
        options = dict(self.config_entry.options)
        options.update(user_input)
        return self.async_create_entry(title="", data=options)


def _load_component_schema(hass, profile: str, data: dict) -> vol.Schema:
    """Return only the mappings required by the selected component profile."""
    discovered = (
        _discover_ems_esp_entities(hass) if profile == LOAD_PROFILE_HEAT_PUMP else {}
    )

    def current(key, fallback=""):
        return data.get(key) or discovered.get(key) or fallback

    common = {
        vol.Required(
            CONF_LOAD_COMPONENT_NAME,
            default=current(
                CONF_LOAD_COMPONENT_NAME,
                {
                    LOAD_PROFILE_HEAT_PUMP: "Heat pump",
                    LOAD_PROFILE_AIR_CONDITIONING: "Air conditioning",
                    LOAD_PROFILE_GENERIC: "Metered load",
                }.get(profile, "Load component"),
            ),
        ): str,
        vol.Required(
            CONF_COMPONENT_POWER_ENTITY,
            default=current(CONF_COMPONENT_POWER_ENTITY),
        ): _entity_selector(["sensor"]),
    }
    if profile == LOAD_PROFILE_HEAT_PUMP:
        common.update(
            {
                vol.Required(
                    CONF_HP_ACTIVITY_ENTITY,
                    default=current(CONF_HP_ACTIVITY_ENTITY),
                ): _entity_selector(["sensor"]),
                vol.Required(
                    CONF_HP_OUTDOOR_TEMP_ENTITY,
                    default=current(CONF_HP_OUTDOOR_TEMP_ENTITY),
                ): _entity_selector(["sensor"]),
                vol.Required(
                    CONF_HP_DHW_TEMP_ENTITY,
                    default=current(CONF_HP_DHW_TEMP_ENTITY),
                ): _entity_selector(["sensor"]),
                vol.Required(
                    CONF_HP_DHW_TARGET_ENTITY,
                    default=current(CONF_HP_DHW_TARGET_ENTITY),
                ): _entity_selector(["sensor", "number"]),
                vol.Required(
                    CONF_HP_DHW_DIFFERENTIAL_ENTITY,
                    default=current(CONF_HP_DHW_DIFFERENTIAL_ENTITY),
                ): _entity_selector(["sensor", "number"]),
                _optional_entity_key(
                    CONF_HP_DHW_CHARGING_ENTITY,
                    current(CONF_HP_DHW_CHARGING_ENTITY),
                ): _entity_selector(["binary_sensor"]),
                _optional_entity_key(
                    CONF_HP_CIRCULATION_ENTITY,
                    current(CONF_HP_CIRCULATION_ENTITY),
                ): _entity_selector(["binary_sensor", "switch"]),
                _optional_entity_key(
                    CONF_HP_HEATING_ACTIVE_ENTITY,
                    current(CONF_HP_HEATING_ACTIVE_ENTITY),
                ): _entity_selector(["binary_sensor"]),
                _optional_entity_key(
                    CONF_HP_TARGET_FLOW_TEMP_ENTITY,
                    current(CONF_HP_TARGET_FLOW_TEMP_ENTITY),
                ): _entity_selector(["sensor", "number"]),
                vol.Required(
                    CONF_DHW_ALLOWED_WINDOWS,
                    default=current(
                        CONF_DHW_ALLOWED_WINDOWS, DEFAULT_DHW_ALLOWED_WINDOWS
                    ),
                ): str,
            }
        )
    elif profile == LOAD_PROFILE_AIR_CONDITIONING:
        common.update(
            {
                vol.Required(
                    CONF_COMPONENT_KEY,
                    default=current(CONF_COMPONENT_KEY, "air_conditioning"),
                ): str,
                vol.Required(
                    CONF_CLIMATE_ENTITIES,
                    default=current(CONF_CLIMATE_ENTITIES, []),
                ): _multiple_entity_selector(["climate"]),
            }
        )
    else:
        common[
            vol.Required(
                CONF_COMPONENT_KEY,
                default=current(CONF_COMPONENT_KEY, "metered_load"),
            )
        ] = str
    return vol.Schema(common)


def _discover_ems_esp_entities(hass) -> dict[str, str]:
    """Suggest known EMS-ESP entities without coupling the runtime adapter."""
    suffixes = {
        CONF_COMPONENT_POWER_ENTITY: "hpcurrpower",
        CONF_HP_ACTIVITY_ENTITY: "hpactivity",
        CONF_HP_OUTDOOR_TEMP_ENTITY: "outdoortemp",
        CONF_HP_DHW_TEMP_ENTITY: "dhw_curtemp",
        CONF_HP_DHW_TARGET_ENTITY: "dhw_settemp",
        CONF_HP_DHW_DIFFERENTIAL_ENTITY: "dhw_ecoplusdiff",
        CONF_HP_DHW_CHARGING_ENTITY: "dhw_charging",
        CONF_HP_CIRCULATION_ENTITY: "dhw_circ",
        CONF_HP_HEATING_ACTIVE_ENTITY: "heatingactive",
        CONF_HP_TARGET_FLOW_TEMP_ENTITY: "targetflowtemp",
    }
    result = {}
    for key, suffix in suffixes.items():
        matches = [
            entity_id
            for entity_id in hass.states.async_entity_ids()
            if entity_id.endswith(suffix)
        ]
        if len(matches) == 1:
            result[key] = matches[0]
    return result


def _entity_schema(data: dict[str, str]) -> vol.Schema:
    """Return entity mapping schema."""
    schema = {
        vol.Required(
            CONF_GRID_MODE, default=data.get(CONF_GRID_MODE, GRID_MODE_THREE_PHASE)
        ): _select_selector(
            [GRID_MODE_SIGNED, GRID_MODE_THREE_PHASE, GRID_MODE_IMPORT_EXPORT]
        ),
        vol.Required(
            CONF_BATTERY_PROFILE,
            default=data.get(CONF_BATTERY_PROFILE, BATTERY_PROFILE_ZENDURE),
        ): _select_selector([BATTERY_PROFILE_ZENDURE, BATTERY_PROFILE_GENERIC]),
        _optional_entity_key(
            CONF_SIGNED_GRID_POWER_ENTITY,
            data.get(CONF_SIGNED_GRID_POWER_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_GRID_L1_ENTITY, data.get(CONF_GRID_L1_ENTITY, "")
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_GRID_L2_ENTITY, data.get(CONF_GRID_L2_ENTITY, "")
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_GRID_L3_ENTITY, data.get(CONF_GRID_L3_ENTITY, "")
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_GRID_IMPORT_ENTITY, data.get(CONF_GRID_IMPORT_ENTITY, "")
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_GRID_EXPORT_ENTITY, data.get(CONF_GRID_EXPORT_ENTITY, "")
        ): _entity_selector(["sensor"]),
        vol.Required(
            CONF_PV_POWER_ENTITY, default=data.get(CONF_PV_POWER_ENTITY, "")
        ): _entity_selector(["sensor"]),
        vol.Required(
            CONF_PRICE_ENTITY, default=data.get(CONF_PRICE_ENTITY, "")
        ): _entity_selector(["sensor"]),
        vol.Required(
            CONF_BATTERY_SOC_ENTITY, default=data.get(CONF_BATTERY_SOC_ENTITY, "")
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_BATTERY_INPUT_ENERGY_ENTITY,
            data.get(CONF_BATTERY_INPUT_ENERGY_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
            data.get(CONF_BATTERY_OUTPUT_ENERGY_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_EV_POWER_ENTITY, data.get(CONF_EV_POWER_ENTITY, "")
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_BATTERY_POWER_ENTITY,
            data.get(CONF_BATTERY_POWER_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_ZENDURE_AC_MODE_ENTITY,
            data.get(CONF_ZENDURE_AC_MODE_ENTITY, ""),
        ): _entity_selector(["select"]),
        _optional_entity_key(
            CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY,
            data.get(CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_ZENDURE_PACK_INPUT_POWER_ENTITY,
            data.get(CONF_ZENDURE_PACK_INPUT_POWER_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
            data.get(CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
            data.get(CONF_ZENDURE_GRID_INPUT_POWER_ENTITY, ""),
        ): _entity_selector(["sensor"]),
        _optional_entity_key(
            CONF_ZENDURE_INPUT_LIMIT_ENTITY,
            data.get(CONF_ZENDURE_INPUT_LIMIT_ENTITY, ""),
        ): _entity_selector(["number"]),
        _optional_entity_key(
            CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
            data.get(CONF_ZENDURE_OUTPUT_LIMIT_ENTITY, ""),
        ): _entity_selector(["number"]),
    }
    return vol.Schema(schema)

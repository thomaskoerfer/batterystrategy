"""Config flow for Battery Strategy."""

from __future__ import annotations

try:
    import voluptuous as vol
    from homeassistant import config_entries
    from homeassistant.helpers import selector
except (
    ImportError
):  # pragma: no cover - allows importing constants in unit tests without HA.
    vol = None
    config_entries = None
    selector = None

from .component_config import DEFAULT_DHW_ALLOWED_WINDOWS, validate_allowed_windows
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

if config_entries is not None:

    def _entity_selector(domains: list[str] | None = None):
        """Return a Home Assistant entity selector."""
        if selector is None:
            return str
        return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains))

    def _multiple_entity_selector(domains: list[str]):
        """Return a multiple Home Assistant entity selector."""
        if selector is None:
            return list
        return selector.EntitySelector(
            selector.EntitySelectorConfig(domain=domains, multiple=True)
        )

    def _select_selector(options: list[str]):
        """Return a select selector."""
        if selector is None:
            return vol.In(options)
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )

    def _translated_select_selector(options: list[str], translation_key: str):
        """Return a select whose labels come from integration translations."""
        if selector is None:
            return vol.In(options)
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

    def _optional_entity_key(key: str, value: str):
        """Return an optional entity field that can also be cleared."""
        description = {"suggested_value": value} if value else None
        return vol.Optional(key, description=description)

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

    def _default_entry_data() -> dict[str, str]:
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

    def _required_entity_keys(data: dict[str, str]) -> set[str]:
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
            required.update(
                (CONF_GRID_L1_ENTITY, CONF_GRID_L2_ENTITY, CONF_GRID_L3_ENTITY)
            )

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

    def _validate_entity_mapping(hass, data: dict[str, str]) -> dict[str, str]:
        """Validate profile completeness and measurement units before saving."""
        errors = {
            key: "required" for key in _required_entity_keys(data) if not data.get(key)
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
        energy_keys = {
            CONF_BATTERY_INPUT_ENERGY_ENTITY,
            CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
        }
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
            if key not in entity_keys:
                continue
            if not entity_id or key in errors:
                continue
            state = hass.states.get(entity_id)
            if state is None:
                errors[key] = "entity_not_found"
                continue
            unit = (
                str(state.attributes.get("unit_of_measurement") or "").strip().lower()
            )
            if key in power_keys and unit not in {"w", "kw", "mw"}:
                errors[key] = "invalid_power_unit"
            elif key in energy_keys and unit != "kwh":
                # Savings consumes recorder counter deltas directly in kWh. Reject
                # Wh counters rather than silently scaling history incorrectly.
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
                return self.async_create_entry(
                    title="Battery Strategy", data=user_input
                )

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
                        default=options.get("strategy_enabled", False),
                    ): bool,
                    vol.Required(
                        "trace_enabled", default=options.get("trace_enabled", False)
                    ): bool,
                    vol.Required(
                        "pv_charging",
                        default=options.get("pv_charging", PV_CHARGING_ON),
                    ): _select_selector([PV_CHARGING_OFF, PV_CHARGING_ON]),
                    vol.Required(
                        "grid_charging",
                        default=options.get("grid_charging", GRID_CHARGING_OFF),
                    ): _select_selector(
                        [GRID_CHARGING_OFF, GRID_CHARGING_PRICE_SENSITIVE]
                    ),
                    vol.Required(
                        "discharge", default=options.get("discharge", DISCHARGE_LOAD)
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
                        default=options.get("pv_to_ev_first", True),
                    ): bool,
                    vol.Required(
                        "discharge_during_ev_charging",
                        default=options.get("discharge_during_ev_charging", True),
                    ): bool,
                    vol.Required(
                        "battery_may_feed_ev",
                        default=options.get("battery_may_feed_ev", False),
                    ): bool,
                    vol.Required(
                        "ev_active_threshold_w",
                        default=options.get("ev_active_threshold_w", 300.0),
                    ): _number_selector(0, 5000, 50, "W"),
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
                        "min_soc_pct", default=options.get("min_soc_pct", 10.0)
                    ): _number_selector(0, 100, 1, "%"),
                    vol.Required(
                        "max_soc_pct", default=options.get("max_soc_pct", 100.0)
                    ): _number_selector(0, 100, 1, "%"),
                    vol.Required(
                        "max_charge_power_w",
                        default=options.get("max_charge_power_w", 2400.0),
                    ): _number_selector(0, 2400, 50, "W"),
                    vol.Required(
                        "max_discharge_power_w",
                        default=options.get("max_discharge_power_w", 2400.0),
                    ): _number_selector(0, 2400, 50, "W"),
                    vol.Required(
                        "min_command_power_w",
                        default=options.get("min_command_power_w", 20.0),
                    ): _number_selector(0, 500, 10, "W"),
                    vol.Required(
                        "min_command_delta_w",
                        default=options.get("min_command_delta_w", 5.0),
                    ): _number_selector(0, 500, 5, "W"),
                    vol.Required(
                        "round_trip_efficiency",
                        default=options.get("round_trip_efficiency", 0.80),
                    ): _number_selector(0.5, 1.0, 0.01),
                    vol.Required(
                        "min_margin_ct_per_kwh",
                        default=options.get("min_margin_ct_per_kwh", 2.0),
                    ): _number_selector(0, 30, 0.1, "ct/kWh"),
                    vol.Required(
                        "planning_horizon_h",
                        default=options.get("planning_horizon_h", 48),
                    ): _number_selector(1, 48, 1, "h"),
                    vol.Required(
                        "feed_in_tariff_ct_per_kwh",
                        default=options.get("feed_in_tariff_ct_per_kwh", 0.0),
                    ): _number_selector(0, 50, 0.1, "ct/kWh"),
                    vol.Required(
                        CONF_BATTERY_CAPACITY_KWH,
                        default=options.get(CONF_BATTERY_CAPACITY_KWH, 6.0),
                    ): _number_selector(0.5, 100, 0.1, "kWh"),
                    vol.Required(
                        CONF_PV_CAPACITY_KWP,
                        default=options.get(CONF_PV_CAPACITY_KWP, 0.0),
                    ): _number_selector(0, 100, 0.1, "kWp"),
                    vol.Required(
                        CONF_PV_INVERTER_POWER_KW,
                        default=options.get(CONF_PV_INVERTER_POWER_KW, 0.0),
                    ): _number_selector(0, 100, 0.1, "kW"),
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
                        "manual_mode", default=options.get("manual_mode", MANUAL_OFF)
                    ): _select_selector([MANUAL_OFF, MANUAL_CHARGE, MANUAL_DISCHARGE]),
                    vol.Required(
                        "manual_power_w", default=options.get("manual_power_w", 0.0)
                    ): _number_selector(0, 2400, 50, "W"),
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
            _discover_ems_esp_entities(hass)
            if profile == LOAD_PROFILE_HEAT_PUMP
            else {}
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

    def _validate_load_component(hass, profile: str, data: dict) -> dict[str, str]:
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

    def _load_component_unique_id(profile: str, data: dict) -> str:
        """Return stable subentry identity independent of its display name."""
        key = (
            "heat_pump"
            if profile == LOAD_PROFILE_HEAT_PUMP
            else str(data.get(CONF_COMPONENT_KEY, "metered_load"))
        )
        return f"{profile}:{key}"

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

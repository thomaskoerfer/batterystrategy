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

from .const import (
    BATTERY_PROFILE_GENERIC,
    BATTERY_PROFILE_ZENDURE,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_INPUT_ENERGY_ENTITY,
    CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_PROFILE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_GRID_L1_ENTITY,
    CONF_GRID_L2_ENTITY,
    CONF_GRID_L3_ENTITY,
    CONF_GRID_MODE,
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
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    MANUAL_OFF,
    PV_CHARGING_OFF,
    PV_CHARGING_ON,
)

if config_entries is not None:

    def _entity_selector(domains: list[str] | None = None):
        """Return a Home Assistant entity selector."""
        if selector is None:
            return str
        return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains))

    def _select_selector(options: list[str]):
        """Return a select selector."""
        if selector is None:
            return vol.In(options)
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN
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
                        default=options.get("min_command_delta_w", 20.0),
                    ): _number_selector(0, 500, 10, "W"),
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
            vol.Optional(
                CONF_SIGNED_GRID_POWER_ENTITY,
                default=data.get(CONF_SIGNED_GRID_POWER_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_GRID_L1_ENTITY, default=data.get(CONF_GRID_L1_ENTITY, "")
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_GRID_L2_ENTITY, default=data.get(CONF_GRID_L2_ENTITY, "")
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_GRID_L3_ENTITY, default=data.get(CONF_GRID_L3_ENTITY, "")
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_GRID_IMPORT_ENTITY, default=data.get(CONF_GRID_IMPORT_ENTITY, "")
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_GRID_EXPORT_ENTITY, default=data.get(CONF_GRID_EXPORT_ENTITY, "")
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
            vol.Optional(
                CONF_BATTERY_INPUT_ENERGY_ENTITY,
                default=data.get(CONF_BATTERY_INPUT_ENERGY_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
                default=data.get(CONF_BATTERY_OUTPUT_ENERGY_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_EV_POWER_ENTITY, default=data.get(CONF_EV_POWER_ENTITY, "")
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_BATTERY_POWER_ENTITY,
                default=data.get(CONF_BATTERY_POWER_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_ZENDURE_AC_MODE_ENTITY,
                default=data.get(CONF_ZENDURE_AC_MODE_ENTITY, ""),
            ): _entity_selector(["select"]),
            vol.Optional(
                CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY,
                default=data.get(CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_ZENDURE_PACK_INPUT_POWER_ENTITY,
                default=data.get(CONF_ZENDURE_PACK_INPUT_POWER_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
                default=data.get(CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
                default=data.get(CONF_ZENDURE_GRID_INPUT_POWER_ENTITY, ""),
            ): _entity_selector(["sensor"]),
            vol.Optional(
                CONF_ZENDURE_INPUT_LIMIT_ENTITY,
                default=data.get(CONF_ZENDURE_INPUT_LIMIT_ENTITY, ""),
            ): _entity_selector(["number"]),
            vol.Optional(
                CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
                default=data.get(CONF_ZENDURE_OUTPUT_LIMIT_ENTITY, ""),
            ): _entity_selector(["number"]),
        }
        return vol.Schema(schema)

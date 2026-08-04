"""Constants for Battery Strategy."""

from __future__ import annotations

DOMAIN = "battery_strategy"
CONFIG_ENTRY_VERSION = 2

CONF_GRID_MODE = "grid_mode"
CONF_BATTERY_PROFILE = "battery_profile"
CONF_SIGNED_GRID_POWER_ENTITY = "signed_grid_power_entity"
CONF_GRID_L1_ENTITY = "grid_l1_entity"
CONF_GRID_L2_ENTITY = "grid_l2_entity"
CONF_GRID_L3_ENTITY = "grid_l3_entity"
CONF_GRID_IMPORT_ENTITY = "grid_import_entity"
CONF_GRID_EXPORT_ENTITY = "grid_export_entity"
CONF_PV_POWER_ENTITY = "pv_power_entity"
CONF_PRICE_ENTITY = "price_entity"
CONF_EV_POWER_ENTITY = "ev_power_entity"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_POWER_ENTITY = "battery_power_entity"
CONF_BATTERY_INPUT_ENERGY_ENTITY = "battery_input_energy_entity"
CONF_BATTERY_OUTPUT_ENERGY_ENTITY = "battery_output_energy_entity"
CONF_ZENDURE_AC_MODE_ENTITY = "zendure_ac_mode_entity"
CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY = "zendure_output_pack_power_entity"
CONF_ZENDURE_PACK_INPUT_POWER_ENTITY = "zendure_pack_input_power_entity"
CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY = "zendure_output_home_power_entity"
CONF_ZENDURE_GRID_INPUT_POWER_ENTITY = "zendure_grid_input_power_entity"
CONF_ZENDURE_INPUT_LIMIT_ENTITY = "zendure_input_limit_entity"
CONF_ZENDURE_OUTPUT_LIMIT_ENTITY = "zendure_output_limit_entity"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_PV_CAPACITY_KWP = "pv_capacity_kwp"
CONF_PV_INVERTER_POWER_KW = "pv_inverter_power_kw"

GRID_MODE_SIGNED = "signed"
GRID_MODE_THREE_PHASE = "three_phase"
GRID_MODE_IMPORT_EXPORT = "import_export"

BATTERY_PROFILE_GENERIC = "generic"
BATTERY_PROFILE_ZENDURE = "zendure"

PV_CHARGING_OFF = "off"
PV_CHARGING_ON = "on"

GRID_CHARGING_OFF = "off"
GRID_CHARGING_PRICE_SENSITIVE = "price_sensitive"

DISCHARGE_OFF = "off"
DISCHARGE_LOAD = "load"
DISCHARGE_PRICE_SENSITIVE = "price_sensitive"

MANUAL_OFF = "off"
MANUAL_CHARGE = "charge"
MANUAL_DISCHARGE = "discharge"

COMMAND_IDLE = "idle"
COMMAND_INPUT = "input"
COMMAND_OUTPUT = "output"

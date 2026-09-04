"""Number controls for Battery Strategy."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity

from .config_definitions import NUMERIC_OPTIONS, NumericOptionDefinition
from .entity import BatteryStrategyEntity

NUMBER_NAMES = {
    "manual_power_w": "Manuelle Leistung",
    "ev_active_threshold_w": "Auto laedt ab",
    "min_soc_pct": "Minimaler SoC",
    "max_soc_pct": "Maximaler SoC",
    "max_charge_power_w": "Max Ladeleistung",
    "max_discharge_power_w": "Max Entladeleistung",
    "min_command_power_w": "Min Befehlsleistung",
    "min_command_delta_w": "Min Befehlsaenderung",
    "round_trip_efficiency": "Roundtrip Wirkungsgrad",
    "min_margin_ct_per_kwh": "Mindestmarge",
    "feed_in_tariff_ct_per_kwh": "Einspeiseverguetung",
    "planning_horizon_h": "Planungshorizont",
}
NUMBERS = tuple(
    definition
    for definition in NUMERIC_OPTIONS.values()
    if definition.exposed_as_entity
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up Battery Strategy number controls."""
    coordinator = entry.runtime_data
    async_add_entities(
        BatteryStrategyNumber(coordinator, definition) for definition in NUMBERS
    )


class BatteryStrategyNumber(BatteryStrategyEntity, NumberEntity):
    """Config-entry backed number control."""

    def __init__(self, coordinator, definition: NumericOptionDefinition):
        """Initialize number control."""
        super().__init__(
            coordinator,
            f"control_{definition.key}",
            NUMBER_NAMES[definition.key],
        )
        self._description = definition
        self._attr_native_min_value = definition.minimum
        self._attr_native_max_value = definition.maximum
        self._attr_native_step = definition.step
        self._attr_native_unit_of_measurement = definition.unit

    @property
    def native_value(self) -> float:
        """Return current numeric value."""
        return float(self._option(self._description.key, self._description.default))

    async def async_set_native_value(self, value: float) -> None:
        """Persist numeric value."""
        if self._description.key == "planning_horizon_h":
            value = int(value)
        await self._set_option(self._description.key, value)

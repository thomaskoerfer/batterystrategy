"""Number controls for Battery Strategy."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity
from homeassistant.const import UnitOfPower

try:
    from homeassistant.const import PERCENTAGE
except ImportError:  # pragma: no cover - compatibility with older HA versions.
    from homeassistant.const import PERCENT as PERCENTAGE

from .const import DOMAIN
from .entity import BatteryStrategyEntity


@dataclass(frozen=True)
class NumberControl:
    """Description for a number control."""

    key: str
    name: str
    default: float
    minimum: float
    maximum: float
    step: float
    unit: str | None = None


NUMBERS = (
    NumberControl(
        "manual_power_w", "Manuelle Leistung", 0.0, 0.0, 2400.0, 50.0, UnitOfPower.WATT
    ),
    NumberControl(
        "ev_active_threshold_w",
        "Auto laedt ab",
        300.0,
        0.0,
        11000.0,
        50.0,
        UnitOfPower.WATT,
    ),
    NumberControl("min_soc_pct", "Minimaler SoC", 10.0, 0.0, 100.0, 1.0, PERCENTAGE),
    NumberControl("max_soc_pct", "Maximaler SoC", 100.0, 0.0, 100.0, 1.0, PERCENTAGE),
    NumberControl(
        "max_charge_power_w",
        "Max Ladeleistung",
        2400.0,
        0.0,
        2400.0,
        50.0,
        UnitOfPower.WATT,
    ),
    NumberControl(
        "max_discharge_power_w",
        "Max Entladeleistung",
        2400.0,
        0.0,
        2400.0,
        50.0,
        UnitOfPower.WATT,
    ),
    NumberControl(
        "min_command_power_w",
        "Min Befehlsleistung",
        20.0,
        0.0,
        500.0,
        10.0,
        UnitOfPower.WATT,
    ),
    NumberControl(
        "min_command_delta_w",
        "Min Befehlsaenderung",
        20.0,
        0.0,
        500.0,
        10.0,
        UnitOfPower.WATT,
    ),
    NumberControl(
        "round_trip_efficiency", "Roundtrip Wirkungsgrad", 0.80, 0.1, 1.0, 0.01
    ),
    NumberControl(
        "min_margin_ct_per_kwh", "Mindestmarge", 2.0, 0.0, 50.0, 0.1, "ct/kWh"
    ),
    NumberControl(
        "feed_in_tariff_ct_per_kwh",
        "Einspeiseverguetung",
        0.0,
        0.0,
        50.0,
        0.1,
        "ct/kWh",
    ),
    NumberControl("planning_horizon_h", "Planungshorizont", 48.0, 1.0, 48.0, 1.0, "h"),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up Battery Strategy number controls."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BatteryStrategyNumber(coordinator, description) for description in NUMBERS
    )


class BatteryStrategyNumber(BatteryStrategyEntity, NumberEntity):
    """Config-entry backed number control."""

    def __init__(self, coordinator, description: NumberControl):
        """Initialize number control."""
        super().__init__(coordinator, f"control_{description.key}", description.name)
        self._description = description
        self._attr_native_min_value = description.minimum
        self._attr_native_max_value = description.maximum
        self._attr_native_step = description.step
        self._attr_native_unit_of_measurement = description.unit

    @property
    def native_value(self) -> float:
        """Return current numeric value."""
        return float(self._option(self._description.key, self._description.default))

    async def async_set_native_value(self, value: float) -> None:
        """Persist numeric value."""
        if self._description.key == "planning_horizon_h":
            value = int(value)
        await self._set_option(self._description.key, value)

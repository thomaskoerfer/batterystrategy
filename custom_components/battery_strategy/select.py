"""Select controls for Battery Strategy."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity

from .const import (
    DISCHARGE_LOAD,
    DISCHARGE_OFF,
    DISCHARGE_PRICE_SENSITIVE,
    DOMAIN,
    GRID_CHARGING_OFF,
    GRID_CHARGING_PRICE_SENSITIVE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    MANUAL_OFF,
    PV_CHARGING_OFF,
    PV_CHARGING_ON,
)
from .entity import BatteryStrategyEntity

SELECTS = (
    (
        "pv_charging",
        "PV-Ueberschuss laden",
        PV_CHARGING_ON,
        {
            "Ein": PV_CHARGING_ON,
            "Aus": PV_CHARGING_OFF,
        },
    ),
    (
        "grid_charging",
        "Netzladen",
        GRID_CHARGING_OFF,
        {
            "Aus": GRID_CHARGING_OFF,
            "Preissensitiv": GRID_CHARGING_PRICE_SENSITIVE,
        },
    ),
    (
        "discharge",
        "Entladen",
        DISCHARGE_LOAD,
        {
            "Aus": DISCHARGE_OFF,
            "Bei Last": DISCHARGE_LOAD,
            "Preissensitiv": DISCHARGE_PRICE_SENSITIVE,
        },
    ),
    (
        "manual_mode",
        "Manueller Modus",
        MANUAL_OFF,
        {
            "Aus": MANUAL_OFF,
            "Laden": MANUAL_CHARGE,
            "Entladen": MANUAL_DISCHARGE,
        },
    ),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up Battery Strategy select controls."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BatteryStrategySelect(coordinator, key, name, default, options)
        for key, name, default, options in SELECTS
    )


class BatteryStrategySelect(BatteryStrategyEntity, SelectEntity):
    """Config-entry backed select control."""

    def __init__(self, coordinator, key: str, name: str, default: str, option_map: dict[str, str]):
        """Initialize select control."""
        super().__init__(coordinator, f"control_{key}", name)
        self._key = key
        self._default = default
        self._option_map = option_map
        self._reverse_map = {value: label for label, value in option_map.items()}
        self._attr_options = list(option_map)

    @property
    def current_option(self) -> str:
        """Return selected display option."""
        raw = self._option(self._key, self._default)
        return self._reverse_map.get(raw, next(iter(self._option_map)))

    async def async_select_option(self, option: str) -> None:
        """Persist selected option."""
        await self._set_option(self._key, self._option_map[option])

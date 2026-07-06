"""Switch controls for Battery Strategy."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .const import DOMAIN
from .entity import BatteryStrategyEntity


SWITCHES = (
    ("strategy_enabled", "Battery Strategy Steuerung", True),
    ("trace_enabled", "Debug Trace aufzeichnen", False),
    ("pv_to_ev_first", "PV zuerst ins Auto", True),
    ("battery_may_feed_ev", "Batterie darf Auto versorgen", False),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up Battery Strategy switches."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(BatteryStrategySwitch(coordinator, key, name, default) for key, name, default in SWITCHES)


class BatteryStrategySwitch(BatteryStrategyEntity, SwitchEntity):
    """Config-entry backed switch."""

    def __init__(self, coordinator, key: str, name: str, default: bool):
        """Initialize switch."""
        super().__init__(coordinator, f"control_{key}", name)
        self._key = key
        self._default = default

    @property
    def is_on(self) -> bool:
        """Return current switch state."""
        return bool(self._option(self._key, self._default))

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        await self._set_option(self._key, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        await self._set_option(self._key, False)

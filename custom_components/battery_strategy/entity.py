"""Shared entity helpers for Battery Strategy."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class BatteryStrategyEntity(CoordinatorEntity):
    """Base entity for Battery Strategy controls and diagnostics."""

    _attr_has_entity_name = False

    def __init__(self, coordinator, key: str, name: str):
        """Initialize a Battery Strategy entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"{DOMAIN}_{key}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.entry.entry_id)},
            "name": "Battery Strategy",
            "manufacturer": "Battery Strategy",
        }

    def _option(self, key: str, default):
        """Return an option value from the config entry."""
        return self.coordinator.entry.options.get(key, default)

    async def _set_option(self, key: str, value) -> None:
        """Persist one option and refresh the coordinator."""
        options = dict(self.coordinator.entry.options)
        options[key] = value
        self.coordinator.hass.config_entries.async_update_entry(
            self.coordinator.entry,
            options=options,
        )
        await self.coordinator.async_request_refresh()

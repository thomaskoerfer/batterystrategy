"""Battery Strategy custom integration."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

try:
    from homeassistant import config_entries
    from homeassistant.const import Platform
    from homeassistant.helpers import entity_registry as er
except ImportError:  # pragma: no cover - unit tests run without Home Assistant.
    config_entries = None
    Platform = None
    er = None

from .const import DOMAIN
try:
    from .coordinator import BatteryStrategyCoordinator, OPTIMIZER_STATE_FILE, _load_last_known_soc_pct
except ImportError:  # pragma: no cover - unit tests run without Home Assistant.
    BatteryStrategyCoordinator = None
    OPTIMIZER_STATE_FILE = "battery_strategy_hacs_optimizer_state.json"
    _load_last_known_soc_pct = None

PLATFORMS = [] if Platform is None else [Platform.SENSOR, Platform.SELECT, Platform.SWITCH, Platform.NUMBER]


async def async_setup(hass, config) -> bool:
    """Set up YAML import for unattended parallel testing."""
    if config_entries is None:
        return False
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data=config.get(DOMAIN) or {},
            )
        )
    return True


async def async_setup_entry(hass, entry) -> bool:
    """Set up Battery Strategy from a config entry."""
    if BatteryStrategyCoordinator is None:
        return False
    _async_remove_deprecated_entities(hass, entry)
    last_known_soc_pct = await hass.async_add_executor_job(
        _load_last_known_soc_pct,
        Path(hass.config.path(OPTIMIZER_STATE_FILE)),
    )
    coordinator = BatteryStrategyCoordinator(
        hass,
        entry,
        update_interval=timedelta(seconds=10),
        last_known_soc_pct=last_known_soc_pct,
    )
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload Battery Strategy."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


def _async_register_services(hass) -> None:
    """Register minimal runtime services once."""
    if hass.data.setdefault(DOMAIN, {}).get("_services_registered"):
        return

    async def _set_manual_mode(call, mode: str) -> None:
        power = float(call.data.get("power_w", 0.0) or 0.0)
        duration = int(call.data.get("duration_min", 0) or 0)
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if isinstance(coordinator, BatteryStrategyCoordinator):
                coordinator.set_manual_override(mode, power, duration)
                await coordinator.async_request_refresh()

    async def manual_charge(call) -> None:
        await _set_manual_mode(call, "charge")

    async def manual_discharge(call) -> None:
        await _set_manual_mode(call, "discharge")

    async def stop_manual(call) -> None:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if isinstance(coordinator, BatteryStrategyCoordinator):
                coordinator.clear_manual_override()
                await coordinator.async_request_refresh()

    async def recalculate(call) -> None:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if isinstance(coordinator, BatteryStrategyCoordinator):
                await coordinator.async_request_refresh()

    async def reset_statistics(call) -> None:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if isinstance(coordinator, BatteryStrategyCoordinator):
                coordinator.reset_parallel_samples()
                await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "manual_charge", manual_charge)
    hass.services.async_register(DOMAIN, "manual_discharge", manual_discharge)
    hass.services.async_register(DOMAIN, "stop_manual", stop_manual)
    hass.services.async_register(DOMAIN, "recalculate", recalculate)
    hass.services.async_register(DOMAIN, "reset_statistics", reset_statistics)
    hass.data[DOMAIN]["_services_registered"] = True


def _async_remove_deprecated_entities(hass, entry) -> None:
    """Remove public controls that are no longer part of the integration."""
    if er is None:
        return
    registry = er.async_get(hass)
    deprecated = {
        f"{entry.entry_id}_control_send_commands",
        "switch.battery_strategy_hacs_befehle_an_batterie_senden",
    }
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id in deprecated or entity.entity_id in deprecated:
            registry.async_remove(entity.entity_id)
    for entity_id in deprecated:
        if entity_id.startswith("switch.") and registry.async_get(entity_id) is not None:
            registry.async_remove(entity_id)

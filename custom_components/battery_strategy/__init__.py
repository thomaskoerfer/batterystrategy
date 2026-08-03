"""Battery Strategy custom integration."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

try:
    from homeassistant import config_entries
    from homeassistant.const import Platform
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import entity_registry as er
except ImportError:  # pragma: no cover - unit tests run without Home Assistant.
    config_entries = None
    cv = None
    Platform = None
    er = None

from .const import DOMAIN

try:
    from .coordinator import (
        COMMAND_TRACE_FILE,
        OPTIMIZER_STATE_FILE,
        BatteryStrategyCoordinator,
        _load_last_known_soc_pct,
    )
except ImportError:  # pragma: no cover - unit tests run without Home Assistant.
    BatteryStrategyCoordinator = None
    COMMAND_TRACE_FILE = "battery_strategy_command_trace.jsonl"
    OPTIMIZER_STATE_FILE = "battery_strategy_optimizer_state.json"
    _load_last_known_soc_pct = None

PLATFORMS = (
    []
    if Platform is None
    else [Platform.SENSOR, Platform.SELECT, Platform.SWITCH, Platform.NUMBER]
)
CONFIG_SCHEMA = None if cv is None else cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config) -> bool:
    """Set up YAML import."""
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
    _async_clean_deprecated_options(hass, entry)
    _async_remove_deprecated_entities(hass, entry)
    await hass.async_add_executor_job(_migrate_runtime_files, hass.config.config_dir)
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


def _migrate_runtime_files(config_dir: str) -> None:
    """Preserve learned state and traces from pre-release filenames."""
    current = Path(config_dir) / OPTIMIZER_STATE_FILE
    legacy = Path(config_dir) / "battery_strategy_hacs_optimizer_state.json"
    if not current.exists() and legacy.exists():
        current.write_bytes(legacy.read_bytes())

    trace = Path(config_dir) / COMMAND_TRACE_FILE
    if trace.exists():
        return
    for legacy_name in (
        "battery_strategy_command_trace.json",
        "battery_strategy_hacs_command_trace.json",
    ):
        legacy_trace = Path(config_dir) / legacy_name
        if not legacy_trace.exists():
            continue
        try:
            payload = json.loads(legacy_trace.read_text(encoding="utf-8"))
            items = payload if isinstance(payload, list) else payload.get("trace", [])
            with trace.open("w", encoding="utf-8") as handle:
                for item in items[-60480:]:
                    handle.write(json.dumps(item, separators=(",", ":")) + "\n")
        except (OSError, ValueError, AttributeError):
            continue
        legacy_trace.unlink(missing_ok=True)
        break


async def async_unload_entry(hass, entry) -> bool:
    """Unload Battery Strategy."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_shutdown()
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

    hass.services.async_register(DOMAIN, "manual_charge", manual_charge)
    hass.services.async_register(DOMAIN, "manual_discharge", manual_discharge)
    hass.services.async_register(DOMAIN, "stop_manual", stop_manual)
    hass.services.async_register(DOMAIN, "recalculate", recalculate)
    hass.data[DOMAIN]["_services_registered"] = True


def _async_remove_deprecated_entities(hass, entry) -> None:
    """Remove public controls that are no longer part of the integration."""
    if er is None:
        return
    registry = er.async_get(hass)
    deprecated = {
        f"{entry.entry_id}_control_send_commands",
        "switch.battery_strategy_hacs_befehle_an_batterie_senden",
        *(
            f"{entry.entry_id}_{key}"
            for key in (
                "parallel_samples",
                "parallel_mode_match",
                "parallel_max_power_delta",
                "parallel_passed",
                "parallel_input_samples",
                "parallel_command_passed",
                "parallel_max_house_load_no_ev_delta",
                "parallel_max_house_load_total_delta",
                "parallel_max_pv_delta",
                "parallel_max_residual_no_ev_delta",
                "parallel_max_residual_with_ev_delta",
                "plan_input_passed",
                "tomorrow_strategy_passed",
                "forty8h_strategy_passed",
                "live_command_passed",
                "override_active",
                "control_pv_to_ev_first",
                "plan_max_tomorrow_power_delta",
                "plan_max_48h_power_delta",
            )
        ),
    }
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id in deprecated or entity.entity_id in deprecated:
            registry.async_remove(entity.entity_id)
    for entity_id in deprecated:
        if (
            entity_id.startswith("switch.")
            and registry.async_get(entity_id) is not None
        ):
            registry.async_remove(entity_id)


def _async_clean_deprecated_options(hass, entry) -> None:
    """Drop options that were exposed but never controlled distinct behavior."""
    options = dict(entry.options)
    changed = False
    for key in ("manual_duration_min", "pv_to_ev_first"):
        if key in options:
            options.pop(key)
            changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, options=options)

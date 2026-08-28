"""Battery Strategy custom integration."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

try:
    from homeassistant.const import Platform
    from homeassistant.helpers import entity_registry as er
except ImportError:  # pragma: no cover - unit tests run without Home Assistant.
    Platform = None
    er = None

from .const import CONFIG_ENTRY_VERSION, DOMAIN
from .optimizer_state import runtime_snapshot

try:
    from .coordinator import (
        COMMAND_TRACE_FILE,
        FEATURE_STORE_FILE,
        OPTIMIZER_STATE_FILE,
        BatteryStrategyCoordinator,
        _load_last_known_soc_pct,
    )
except ImportError:  # pragma: no cover - unit tests run without Home Assistant.
    BatteryStrategyCoordinator = None
    COMMAND_TRACE_FILE = "battery_strategy_command_trace.jsonl"
    FEATURE_STORE_FILE = "battery_strategy_features.json.gz"
    OPTIMIZER_STATE_FILE = "battery_strategy_optimizer_state.json"
    _load_last_known_soc_pct = None

PLATFORMS = (
    []
    if Platform is None
    else [Platform.SENSOR, Platform.SELECT, Platform.SWITCH, Platform.NUMBER]
)


async def async_setup_entry(hass, entry) -> bool:
    """Set up Battery Strategy from a config entry."""
    if BatteryStrategyCoordinator is None:
        return False
    _async_remove_deprecated_entities(hass, entry)
    await hass.async_add_executor_job(_migrate_runtime_files, hass.config.config_dir)
    last_known_soc_pct, last_optimizer_output = await hass.async_add_executor_job(
        runtime_snapshot,
        Path(hass.config.path(OPTIMIZER_STATE_FILE)),
    )
    from .feature_store import CompressedFeatureStore, ExecutorFeatureStore

    feature_store = CompressedFeatureStore(Path(hass.config.path(FEATURE_STORE_FILE)))
    await hass.async_add_executor_job(feature_store.initialize)
    feature_history = await hass.async_add_executor_job(
        feature_store.load, 0, 2**63 - 1
    )
    coordinator = BatteryStrategyCoordinator(
        hass,
        entry,
        update_interval=timedelta(seconds=10),
        last_known_soc_pct=last_known_soc_pct,
        last_optimizer_output=last_optimizer_output,
        feature_store=ExecutorFeatureStore(feature_store, hass.async_add_executor_job),
        feature_history=feature_history,
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
        await coordinator.async_prepare_unload()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def async_migrate_entry(hass, entry) -> bool:
    """Migrate stored options without changing active strategy semantics."""
    if entry.version > CONFIG_ENTRY_VERSION:
        return False
    if entry.version == CONFIG_ENTRY_VERSION:
        return True

    options = dict(entry.options)
    options.pop("manual_duration_min", None)
    # beta.4 accidentally removed this policy. Persist the safe historic default
    # so future defaults cannot silently change an upgraded installation.
    options.setdefault("pv_to_ev_first", True)
    hass.config_entries.async_update_entry(
        entry,
        options=options,
        version=CONFIG_ENTRY_VERSION,
    )
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

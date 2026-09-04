"""Battery Strategy custom integration."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .command_trace import COMMAND_TRACE_FILE
from .compiler_runtime_store import CompilerRuntimeStore
from .const import CONFIG_ENTRY_VERSION, DOMAIN
from .coordinator import (
    FEATURE_STORE_FILE,
    OPTIMIZER_STATE_FILE,
    BatteryStrategyCoordinator,
)
from .planning_state import PlanningStateStore

PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.SWITCH, Platform.NUMBER]
type BatteryStrategyConfigEntry = ConfigEntry[BatteryStrategyCoordinator]


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Register integration-level services once during domain setup."""
    _async_register_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: BatteryStrategyConfigEntry
) -> bool:
    """Set up Battery Strategy from a config entry."""
    _async_remove_deprecated_entities(hass, entry)
    await hass.async_add_executor_job(_migrate_runtime_files, hass.config.config_dir)
    planning_state_store = PlanningStateStore.claim(
        str(Path(hass.config.path(OPTIMIZER_STATE_FILE)))
    )
    last_known_soc_pct, last_optimizer_output = await hass.async_add_executor_job(
        planning_state_store.runtime_snapshot,
    )
    from .feature_store import CompressedFeatureStore, ExecutorFeatureStore

    feature_store = CompressedFeatureStore(Path(hass.config.path(FEATURE_STORE_FILE)))
    await hass.async_add_executor_job(feature_store.initialize)
    feature_history = await hass.async_add_executor_job(
        feature_store.load, 0, 2**63 - 1
    )
    compiler_runtime_store = CompilerRuntimeStore(hass, entry.entry_id)
    restored_compiler_runtime = await compiler_runtime_store.load()
    coordinator = BatteryStrategyCoordinator(
        hass,
        entry,
        update_interval=timedelta(seconds=10),
        last_known_soc_pct=last_known_soc_pct,
        last_optimizer_output=last_optimizer_output,
        feature_store=ExecutorFeatureStore(feature_store, hass.async_add_executor_job),
        feature_history=feature_history,
        compiler_runtime_store=compiler_runtime_store,
        restored_compiler_runtime=restored_compiler_runtime,
        planning_state_store=planning_state_store,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start_live_tracking()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _migrate_runtime_files(config_dir: str) -> None:
    """Upgrade persisted data formats without retaining alternate runtime paths."""
    root = Path(config_dir)
    # Completed evaluation windows are not runtime dependencies. Remove their
    # bounded traces during upgrade instead of retaining permanent dead state.
    for obsolete_name in (
        "battery_strategy_optimizer_shadow.jsonl",
        "battery_strategy_compiler_shadow.jsonl",
        "battery_strategy_forecast_shadow.json.gz",
    ):
        (root / obsolete_name).unlink(missing_ok=True)
    current = root / OPTIMIZER_STATE_FILE
    previous_state = root / "battery_strategy_hacs_optimizer_state.json"
    if not current.exists() and previous_state.exists():
        current.write_bytes(previous_state.read_bytes())
    if current.exists():
        previous_state.unlink(missing_ok=True)

    trace = root / COMMAND_TRACE_FILE
    if trace.exists():
        return
    for previous_name in (
        "battery_strategy_command_trace.json",
        "battery_strategy_hacs_command_trace.json",
    ):
        previous_trace = Path(config_dir) / previous_name
        if not previous_trace.exists():
            continue
        try:
            payload = json.loads(previous_trace.read_text(encoding="utf-8"))
            items = payload if isinstance(payload, list) else payload.get("trace", [])
            with trace.open("w", encoding="utf-8") as handle:
                for item in items[-60480:]:
                    handle.write(json.dumps(item, separators=(",", ":")) + "\n")
        except OSError, ValueError, AttributeError:
            continue
        previous_trace.unlink(missing_ok=True)
        break


async def async_unload_entry(
    hass: HomeAssistant, entry: BatteryStrategyConfigEntry
) -> bool:
    """Unload Battery Strategy."""
    coordinator = entry.runtime_data
    if not await coordinator.async_prepare_unload():
        return False
    try:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Keep the coordinator alive when a platform unload raises so it can resume.
    except Exception:
        await coordinator.async_abort_unload()
        raise
    if not unload_ok:
        await coordinator.async_abort_unload()
    else:
        coordinator.finalize_unload()
    return bool(unload_ok)


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
    if hass.services.has_service(DOMAIN, "recalculate"):
        return

    async def _set_manual_mode(call, mode: str) -> None:
        power = float(call.data.get("power_w", 0.0) or 0.0)
        duration = int(call.data.get("duration_min", 0) or 0)
        for coordinator in _coordinators(hass):
            coordinator.set_manual_override(mode, power, duration)
            await coordinator.async_request_refresh()

    async def manual_charge(call) -> None:
        await _set_manual_mode(call, "charge")

    async def manual_discharge(call) -> None:
        await _set_manual_mode(call, "discharge")

    async def stop_manual(call) -> None:
        for coordinator in _coordinators(hass):
            coordinator.clear_manual_override()
            await coordinator.async_request_refresh()

    async def recalculate(call) -> None:
        for coordinator in _coordinators(hass):
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "manual_charge", manual_charge)
    hass.services.async_register(DOMAIN, "manual_discharge", manual_discharge)
    hass.services.async_register(DOMAIN, "stop_manual", stop_manual)
    hass.services.async_register(DOMAIN, "recalculate", recalculate)


def _coordinators(hass: HomeAssistant) -> Iterable[BatteryStrategyCoordinator]:
    """Yield loaded coordinators without maintaining a secondary registry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if isinstance(coordinator, BatteryStrategyCoordinator):
            yield coordinator


def _async_remove_deprecated_entities(hass, entry) -> None:
    """Remove public controls that are no longer part of the integration."""
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

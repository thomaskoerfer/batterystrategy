"""Battery Strategy custom integration."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .command_trace import COMMAND_TRACE_FILE
from .compiler_runtime_store import CompilerRuntimeStore
from .component_history_adapter import async_backfill_cyclic_component_history
from .const import (
    CONF_HP_DHW_DIFFERENTIAL_ENTITY,
    CONF_HP_DHW_TARGET_ENTITY,
    CONF_LOAD_COMPONENT_PROFILE,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    LOAD_PROFILE_HEAT_PUMP,
)
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
    # Retry the semantic role correction during every setup. Config-entry
    # migration can run before the source integration has restored its states.
    _migrate_ems_esp_dhw_cutout_mapping(hass, entry)
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
    executor_feature_store = ExecutorFeatureStore(
        feature_store, hass.async_add_executor_job
    )
    compiler_runtime_store = CompilerRuntimeStore(hass, entry.entry_id)
    restored_compiler_runtime = await compiler_runtime_store.load()
    coordinator = BatteryStrategyCoordinator(
        hass,
        entry,
        update_interval=timedelta(seconds=10),
        last_known_soc_pct=last_known_soc_pct,
        last_optimizer_output=last_optimizer_output,
        feature_store=executor_feature_store,
        feature_history=feature_history,
        compiler_runtime_store=compiler_runtime_store,
        restored_compiler_runtime=restored_compiler_runtime,
        planning_state_store=planning_state_store,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start_live_tracking()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_feature_history_update(
        async_backfill_cyclic_component_history(
            hass,
            entry,
            executor_feature_store,
            feature_history,
            as_of=datetime.now(UTC),
        )
    )
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
    if entry.version < 4:
        _migrate_ems_esp_dhw_cutout_mapping(hass, entry)
    hass.config_entries.async_update_entry(
        entry,
        options=options,
        version=CONFIG_ENTRY_VERSION,
    )
    return True


def _migrate_ems_esp_dhw_cutout_mapping(hass, entry) -> None:
    """Map EMS-ESP DHW targets to the stable configured cut-out."""
    subentries = getattr(entry, "subentries", {})
    states = getattr(hass, "states", None)
    if states is None:
        return
    registry = er.async_get(hass)
    entity_ids = set(getattr(states, "async_entity_ids", lambda: ())())
    entity_ids.update(registry.entities)
    for subentry in subentries.values():
        data = dict(subentry.data)
        if data.get(CONF_LOAD_COMPONENT_PROFILE) != LOAD_PROFILE_HEAT_PUMP:
            continue
        source_entity = str(data.get(CONF_HP_DHW_TARGET_ENTITY) or "")
        source_suffix = next(
            (
                suffix
                for suffix in ("dhw_tempecoplus", "dhw_settemp")
                if source_entity.endswith(suffix)
            ),
            None,
        )
        if source_suffix is None:
            continue
        prefix = source_entity.split(".", 1)[-1][: -len(source_suffix)]
        candidates = [
            entity_id
            for entity_id in entity_ids
            if entity_id.split(".", 1)[-1] == f"{prefix}dhw_ecoplusstop"
            and _registered_entity_enabled(registry, entity_id)
        ]
        if len(candidates) != 1:
            continue
        source = _state_float(hass, source_entity)
        differential = _state_float(hass, data.get(CONF_HP_DHW_DIFFERENTIAL_ENTITY))
        cutout = _state_float(hass, candidates[0])
        if source_suffix == "dhw_tempecoplus":
            live_values_verify_role = (
                source is not None
                and differential is not None
                and cutout is not None
                and abs(source + differential - cutout) <= 0.25
            )
        else:
            # dhw_settemp follows the schedule and can fall to a frost-protection
            # value while DHW is blocked. Equality is useful only while enabled.
            live_values_verify_role = (
                source is not None
                and cutout is not None
                and abs(source - cutout) <= 0.25
            )
        if not live_values_verify_role and not _same_registered_device(
            registry, source_entity, candidates[0]
        ):
            continue
        data[CONF_HP_DHW_TARGET_ENTITY] = candidates[0]
        hass.config_entries.async_update_subentry(
            subentry=subentry, entry=entry, data=data
        )


def _same_registered_device(
    registry, first_entity_id: str, second_entity_id: str
) -> bool:
    """Confirm two unavailable migration candidates belong to one device."""
    first = registry.async_get(first_entity_id)
    second = registry.async_get(second_entity_id)
    return bool(
        first is not None
        and second is not None
        and first.device_id is not None
        and first.device_id == second.device_id
    )


def _registered_entity_enabled(registry, entity_id: str) -> bool:
    """Accept live-only candidates and reject disabled registry entities."""
    registered = registry.async_get(entity_id)
    return registered is None or getattr(registered, "disabled_by", None) is None


def _state_float(hass, entity_id) -> float | None:
    state = hass.states.get(entity_id) if entity_id else None
    try:
        value = float(state.state) if state is not None else None
        return value if value is not None and math.isfinite(value) else None
    except TypeError, ValueError:
        return None


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

"""Home Assistant adapter for bounded load-component history bootstrap."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .component_history import backfill_component_power_history
from .const import (
    CONF_COMPONENT_KEY,
    CONF_COMPONENT_POWER_ENTITY,
    CONF_LOAD_COMPONENT_PROFILE,
    LOAD_PROFILE_CYCLIC_APPLIANCE,
    SUBENTRY_TYPE_LOAD_COMPONENT,
)
from .history_adapter import read_recorder_series_with_availability

BACKFILL_DAYS = 21
BACKFILL_STORE_KEY = "battery_strategy.component_history_backfill"
BACKFILL_STORE_VERSION = 1
LOGGER = logging.getLogger(__name__)


async def async_backfill_cyclic_component_history(
    hass: HomeAssistant,
    entry,
    feature_store,
    feature_history,
    *,
    as_of: datetime,
    marker_store=None,
):
    """Seed newly configured cyclic components from bounded Recorder history."""
    entities = _cyclic_component_entities(entry)
    if not entities or not feature_history:
        return feature_history
    marker_store = marker_store or Store(
        hass, BACKFILL_STORE_VERSION, BACKFILL_STORE_KEY
    )
    marker = await marker_store.async_load() or {}
    completed = dict(marker.get("completed") or {})
    fingerprints = {
        key: sha256(entity_id.encode("utf-8")).hexdigest()
        for key, entity_id in entities.items()
    }
    entities = {
        key: entity_id
        for key, entity_id in entities.items()
        if completed.get(key) != fingerprints[key]
    }
    if not entities:
        return feature_history
    end_time = as_of.astimezone(UTC)
    earliest_ms = max(
        feature_history[0].slot.start_ms,
        int((end_time - timedelta(days=BACKFILL_DAYS)).timestamp() * 1000),
    )
    relevant_history = tuple(
        slot for slot in feature_history if slot.slot.start_ms >= earliest_ms
    )
    missing = dict(entities)
    scales = {key: _power_scale(hass, entity_id) for key, entity_id in missing.items()}
    missing = {
        key: entity_id for key, entity_id in missing.items() if scales[key] is not None
    }
    scales = {key: scales[key] for key in missing}
    if not missing:
        return feature_history
    try:
        series = await _await_executor_completion(
            hass,
            partial(
                read_recorder_series_with_availability,
                hass,
                missing,
                scales,
                start_time=datetime.fromtimestamp(earliest_ms / 1000.0, UTC),
                end_time=end_time,
            ),
        )
        changed = backfill_component_power_history(relevant_history, series)
        if changed:
            feature_history = await feature_store.add_missing_load_components(changed)
        completed.update({key: fingerprints[key] for key in missing})
        await marker_store.async_save({"completed": completed})
        return feature_history
    except Exception as err:  # Recorder backfill must not block live control.
        LOGGER.warning("Cyclic appliance history backfill failed: %s", err)
    return feature_history


async def _await_executor_completion(hass: HomeAssistant, target):
    """Keep unload waiting until an uncancellable Recorder query has stopped."""
    task = asyncio.ensure_future(hass.async_add_executor_job(target))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            # The lifecycle cancellation remains authoritative; the next setup
            # retries because the completion marker was not written.
            pass
        raise


def _cyclic_component_entities(entry) -> dict[str, str]:
    result = {}
    for subentry in getattr(entry, "subentries", {}).values():
        if getattr(subentry, "subentry_type", None) != SUBENTRY_TYPE_LOAD_COMPONENT:
            continue
        data = dict(subentry.data)
        if data.get(CONF_LOAD_COMPONENT_PROFILE) != LOAD_PROFILE_CYCLIC_APPLIANCE:
            continue
        key = str(data.get(CONF_COMPONENT_KEY) or "")
        entity_id = str(data.get(CONF_COMPONENT_POWER_ENTITY) or "")
        if key and entity_id:
            result[key] = entity_id
    return result


def _power_scale(hass, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "").strip().lower()
    return {"w": 1.0, "kw": 1_000.0, "mw": 1_000_000.0}.get(unit)

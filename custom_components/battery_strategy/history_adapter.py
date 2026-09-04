"""Home Assistant Recorder adapter for bounded historical state snapshots."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Hashable, TypeVar

RoleT = TypeVar("RoleT", bound=Hashable)


def read_recorder_series(
    hass,
    entity_map: Mapping[RoleT, str],
    entity_scale: Mapping[RoleT, float],
    *,
    start_time: dt.datetime,
    end_time: dt.datetime,
) -> dict[RoleT, tuple[tuple[float, float], ...]]:
    """Read normalized numeric history through Home Assistant's public API."""
    from homeassistant.components.recorder import history

    mapped = {key: value for key, value in entity_map.items() if value}
    if not mapped:
        return {}
    states = history.get_significant_states(
        hass,
        start_time,
        end_time=end_time,
        entity_ids=sorted(set(mapped.values())),
        include_start_time_state=True,
        significant_changes_only=False,
        no_attributes=True,
    )
    result: dict[RoleT, tuple[tuple[float, float], ...]] = {}
    for role, entity_id in mapped.items():
        scale = float(entity_scale.get(role, 1.0))
        values = []
        for state in states.get(entity_id, ()):
            try:
                timestamp = state.last_updated.timestamp()
                value = float(state.state) * scale
            except (AttributeError, TypeError, ValueError):
                continue
            if (
                timestamp > end_time.timestamp()
                or not math.isfinite(timestamp)
                or not math.isfinite(value)
            ):
                continue
            values.append((timestamp, value))
        result[role] = tuple(values)
    return result

"""Recorder adapter tests independent of the configured database backend."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.battery_strategy.history_adapter import read_recorder_series


def test_recorder_adapter_maps_roles_and_units_through_public_history_api():
    timestamp = dt.datetime(2026, 9, 2, 10, tzinfo=dt.timezone.utc)
    state = SimpleNamespace(last_updated=timestamp, state="1.25")
    with patch(
        "homeassistant.components.recorder.history.get_significant_states",
        return_value={"sensor.source": [state]},
    ) as get_states:
        result = read_recorder_series(
            object(),
            {"pv_power": "sensor.source"},
            {"pv_power": 1000.0},
            start_time=timestamp - dt.timedelta(hours=1),
        )

    assert result == {"pv_power": ((timestamp.timestamp(), 1250.0),)}
    assert get_states.call_args.kwargs["significant_changes_only"] is False
    assert get_states.call_args.kwargs["no_attributes"] is True


def test_recorder_adapter_excludes_non_numeric_states():
    timestamp = dt.datetime(2026, 9, 2, 10, tzinfo=dt.timezone.utc)
    states = [
        SimpleNamespace(last_updated=timestamp, state="unknown"),
        SimpleNamespace(last_updated=timestamp, state="2.0"),
    ]
    with patch(
        "homeassistant.components.recorder.history.get_significant_states",
        return_value={"sensor.source": states},
    ):
        result = read_recorder_series(
            object(),
            {"grid_import": "sensor.source"},
            {},
            start_time=timestamp - dt.timedelta(hours=1),
        )

    assert result["grid_import"] == ((timestamp.timestamp(), 2.0),)

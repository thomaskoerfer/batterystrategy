"""Measured-savings boundary tests."""

from __future__ import annotations

import datetime as dt

from custom_components.battery_strategy.planning_state import SavingsState
from custom_components.battery_strategy.savings import (
    SavingsConfig,
    SavingsEntities,
    SavingsLedger,
)

ENTITIES = SavingsEntities(
    price="price",
    battery_input_energy="battery_input",
    battery_output_energy="battery_output",
    grid_import="grid_import",
    grid_export="grid_export",
    battery_power="battery_power",
)


def _ledger(series: dict, prices: list[dict]) -> SavingsLedger:
    return SavingsLedger(
        config=SavingsConfig(dt.timezone.utc, 60, ENTITIES),
        history_reader=lambda entity_ids, cutoff: {
            entity_id: [item for item in series.get(entity_id, []) if item[0] >= cutoff]
            for entity_id in entity_ids
        },
        price_reader=lambda _dates: prices,
    )


def test_grid_charge_is_costed_and_pv_charge_remains_free():
    start = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    start_ts = start.timestamp()
    event_ts = start_ts + 900
    prices = [{"dt": start, "price_eur": 0.20}]

    grid_series = {
        "battery_input": [(start_ts, 10.0), (event_ts, 11.0)],
        "battery_output": [(start_ts, 4.0)],
        "grid_import": [(start_ts, 1000.0), (event_ts, 1000.0)],
        "grid_export": [(start_ts, 0.0), (event_ts, 0.0)],
        "battery_power": [(start_ts, -1000.0), (event_ts, -1000.0)],
        "price": [],
    }
    state = SavingsState(
        tracker={
            "last_ts": start_ts,
            "last_input_kwh": 10.0,
            "last_output_kwh": 4.0,
            "savings_backfill_v1_done": True,
        }
    )
    daily, today, _ = _ledger(grid_series, prices).update(state, event_ts + 60)
    record = daily[start.date().isoformat()]
    assert record["charge_grid_kwh"] == 1.0
    assert record["charge_pv_kwh"] == 0.0
    assert record["charge_cost_eur"] == 0.2
    assert today == -0.2

    pv_series = dict(grid_series)
    pv_series["grid_import"] = [(start_ts, 0.0), (event_ts, 0.0)]
    pv_state = SavingsState(
        tracker={
            "last_ts": start_ts,
            "last_input_kwh": 10.0,
            "last_output_kwh": 4.0,
            "savings_backfill_v1_done": True,
        }
    )
    pv_daily, pv_today, _ = _ledger(pv_series, prices).update(pv_state, event_ts + 60)
    pv_record = pv_daily[start.date().isoformat()]
    assert pv_record["charge_grid_kwh"] == 0.0
    assert pv_record["charge_pv_kwh"] == 1.0
    assert pv_record["charge_cost_eur"] == 0.0
    assert pv_today == 0.0


def test_missing_prices_do_not_advance_counter_tracker():
    now = dt.datetime(2026, 9, 2, 12, tzinfo=dt.timezone.utc).timestamp()
    state = SavingsState(
        tracker={
            "last_ts": now - 900,
            "last_input_kwh": 10.0,
            "last_output_kwh": 4.0,
            "savings_backfill_v1_done": True,
        }
    )
    series = {
        "battery_input": [(now - 900, 10.0), (now, 11.0)],
        "battery_output": [(now - 900, 4.0)],
    }
    _ledger(series, []).update(state, now)
    assert state.tracker["last_ts"] == now - 900
    assert state.tracker["last_input_kwh"] == 10.0

"""Tests for the EV marginal forecast boundary."""

import datetime as dt

from custom_components.battery_strategy.contracts import (
    DataQuality,
    ForecastRequest,
    HistoricalFeatureSlot,
    QualityFlag,
    SlotKey,
)
from custom_components.battery_strategy.forecasting.ev import HistoricalEvForecaster

SLOT_MS = 15 * 60 * 1000


def test_ev_history_is_not_rejected_for_unrelated_missing_price_quality():
    target = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    start_ms = int(target.timestamp() * 1000)
    historical_start = start_ms - 7 * 24 * 60 * 60 * 1000
    history = (
        HistoricalFeatureSlot(
            SlotKey(historical_start, historical_start + SLOT_MS),
            0.2,
            0.0,
            0.2,
            0.0,
            0.0,
            0.0,
            0.4,
            None,
            DataQuality(1.0, (QualityFlag.MISSING_PRICE,)),
        ),
    )

    forecast = HistoricalEvForecaster(0.0, 300.0).forecast(
        ForecastRequest(
            start_ms,
            "UTC",
            (SlotKey(start_ms, start_ms + SLOT_MS),),
        ),
        history,
    )

    assert forecast.slots[0].energy.p50_kwh == 0.4
    assert forecast.slots[0].quality.coverage == 1.0

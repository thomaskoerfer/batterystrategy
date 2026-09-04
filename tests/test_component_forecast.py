"""Tests for independently composed load forecasts."""

from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.component_config import LoadComponentSpec
from custom_components.battery_strategy.const import LOAD_PROFILE_AIR_CONDITIONING
from custom_components.battery_strategy.contracts import (
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadForecastContext,
    SlotKey,
)
from custom_components.battery_strategy.forecasting.components import (
    build_component_load_forecast,
)
from custom_components.battery_strategy.forecasting.history import ForecastTargetInput
from custom_components.battery_strategy.forecasting.load import LoadForecastModelConfig

SLOT_MS = 900_000


def _history(count: int):
    return tuple(
        HistoricalFeatureSlot(
            slot=SlotKey(index * SLOT_MS, (index + 1) * SLOT_MS),
            house_load_no_ev_kwh=0.2,
            pv_generation_kwh=0.0,
            grid_import_kwh=0.2,
            grid_export_kwh=0.0,
            battery_charge_kwh=0.0,
            battery_discharge_kwh=0.0,
            ev_charge_kwh=0.0,
            price_ct_per_kwh=30.0,
            load_components=(LoadComponentEnergy("air_conditioning", 0.05),),
        )
        for index in range(count)
    )


class ComponentForecastTests(unittest.TestCase):
    def test_components_sum_exactly_after_warmup(self):
        history = _history(7 * 96)
        start = history[-1].slot.end_ms
        slot = SlotKey(start, start + SLOT_MS)
        request = ForecastRequest(start, "Europe/Berlin", (slot,))
        local = dt.datetime.fromtimestamp(start / 1000.0, tz=dt.UTC).astimezone(
            ZoneInfo("Europe/Berlin")
        )
        forecast = build_component_load_forecast(
            request,
            history,
            (ForecastTargetInput(local, 1.0),),
            LoadForecastContext(800.0),
            (),
            (LoadComponentSpec("air_conditioning", LOAD_PROFILE_AIR_CONDITIONING),),
            LoadForecastModelConfig("Europe/Berlin", 1.0, (1.0,) * 96),
        )
        self.assertEqual(
            [item.component_key for item in forecast.components],
            ["general_house_load", "air_conditioning"],
        )
        self.assertAlmostEqual(
            forecast.slots[0].energy.p50_kwh,
            sum(item.slots[0].energy.p50_kwh for item in forecast.components),
        )

    def test_warmup_does_not_double_count_current_component(self):
        history = _history(1)
        start = history[-1].slot.end_ms
        slot = SlotKey(start, start + SLOT_MS)
        request = ForecastRequest(start, "Europe/Berlin", (slot,))
        local = dt.datetime.fromtimestamp(start / 1000.0, tz=dt.UTC).astimezone(
            ZoneInfo("Europe/Berlin")
        )
        forecast = build_component_load_forecast(
            request,
            history,
            (ForecastTargetInput(local, 1.0),),
            LoadForecastContext(800.0),
            (),
            (LoadComponentSpec("air_conditioning", LOAD_PROFILE_AIR_CONDITIONING),),
            LoadForecastModelConfig("Europe/Berlin", 1.0, (1.0,) * 96),
        )
        component = next(
            item
            for item in forecast.components
            if item.component_key == "air_conditioning"
        )
        self.assertEqual(component.slots[0].energy.p50_kwh, 0.0)

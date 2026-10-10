"""Tests for independently composed load forecasts."""

from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.component_config import LoadComponentSpec
from custom_components.battery_strategy.const import (
    LOAD_PROFILE_AIR_CONDITIONING,
    LOAD_PROFILE_HEAT_PUMP,
)
from custom_components.battery_strategy.contracts import (
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadDriverSnapshot,
    LoadFeatureValue,
    LoadForecastContext,
    SlotKey,
    WeatherSlot,
)
from custom_components.battery_strategy.forecasting.components import (
    _component_power_w,
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
    def test_production_composition_uses_active_space_heating_model(self):
        start = int(dt.datetime(2026, 10, 20, 6, 0, tzinfo=dt.UTC).timestamp() * 1000)
        history = tuple(
            HistoricalFeatureSlot(
                slot=SlotKey(
                    start - (7 * 96 - index) * SLOT_MS,
                    start - (7 * 96 - index - 1) * SLOT_MS,
                ),
                house_load_no_ev_kwh=0.2,
                pv_generation_kwh=0.0,
                grid_import_kwh=0.2,
                grid_export_kwh=0.0,
                battery_charge_kwh=0.0,
                battery_discharge_kwh=0.0,
                ev_charge_kwh=0.0,
                price_ct_per_kwh=30.0,
                load_components=(
                    LoadComponentEnergy(
                        "heat_pump_dhw",
                        0.0,
                        features=(LoadFeatureValue("dhw_charging_fraction", 0.0),),
                    ),
                    LoadComponentEnergy(
                        "heat_pump_space_heating",
                        0.0,
                        features=(
                            LoadFeatureValue("outdoor_temperature_c", 5.0),
                            LoadFeatureValue("target_flow_temperature_c", 32.0),
                            LoadFeatureValue("heating_active_fraction", 0.0),
                        ),
                    ),
                ),
            )
            for index in range(7 * 96)
        )
        slots = tuple(
            SlotKey(start + index * SLOT_MS, start + (index + 1) * SLOT_MS)
            for index in range(4)
        )
        request = ForecastRequest(start, "Europe/Berlin", slots)
        targets = tuple(
            ForecastTargetInput(
                dt.datetime.fromtimestamp(slot.start_ms / 1000.0, dt.UTC).astimezone(
                    ZoneInfo("Europe/Berlin")
                ),
                1.0,
            )
            for slot in slots
        )
        context = LoadForecastContext(
            1800.0,
            (
                LoadDriverSnapshot("heat_pump_dhw", 0.0),
                LoadDriverSnapshot(
                    "heat_pump_space_heating",
                    1600.0,
                    features=(
                        LoadFeatureValue("outdoor_temperature_c", 5.0),
                        LoadFeatureValue("target_flow_temperature_c", 32.0),
                        LoadFeatureValue("heating_active_fraction", 1.0),
                        LoadFeatureValue("heating_active_age_s", 900.0),
                    ),
                ),
            ),
        )

        forecast = build_component_load_forecast(
            request,
            history,
            targets,
            context,
            tuple(WeatherSlot(slot, temperature_c=5.0) for slot in slots),
            (
                LoadComponentSpec(
                    "heat_pump_dhw", LOAD_PROFILE_HEAT_PUMP, "00:00-23:59"
                ),
                LoadComponentSpec("heat_pump_space_heating", LOAD_PROFILE_HEAT_PUMP),
            ),
            LoadForecastModelConfig("Europe/Berlin", 1.0, (1.0,) * 96),
        )

        heating = next(
            item
            for item in forecast.components
            if item.component_key == "heat_pump_space_heating"
        )
        self.assertIn("space-heating-v2", heating.model_version)
        self.assertGreater(heating.slots[0].energy.p50_kwh, 0.2)
        self.assertAlmostEqual(
            forecast.slots[0].energy.p50_kwh,
            sum(item.slots[0].energy.p50_kwh for item in forecast.components),
        )

    def test_dhw_baseline_uses_only_current_thermostat_regime(self):
        timezone = ZoneInfo("Europe/Berlin")
        target = dt.datetime(2026, 9, 6, 3, 0, tzinfo=timezone)

        def sample(day: int, target_c: float, energy_kwh: float):
            local = dt.datetime(2026, 8, day, 3, 0, tzinfo=timezone)
            slot = SlotKey(
                int(local.astimezone(dt.UTC).timestamp() * 1000),
                int(
                    (local + dt.timedelta(minutes=15)).astimezone(dt.UTC).timestamp()
                    * 1000
                ),
            )
            return HistoricalFeatureSlot(
                slot=slot,
                house_load_no_ev_kwh=energy_kwh,
                pv_generation_kwh=0.0,
                grid_import_kwh=energy_kwh,
                grid_export_kwh=0.0,
                battery_charge_kwh=0.0,
                battery_discharge_kwh=0.0,
                ev_charge_kwh=0.0,
                price_ct_per_kwh=30.0,
                load_components=(
                    LoadComponentEnergy(
                        "heat_pump_dhw",
                        energy_kwh,
                        features=(LoadFeatureValue("dhw_target_c", target_c),),
                    ),
                ),
            )

        history = (
            *(sample(day, 44.0, 0.75) for day in (9, 16, 23, 30)),
            sample(30, 53.0, 0.25),
        )
        request = ForecastRequest(
            int(target.astimezone(dt.UTC).timestamp() * 1000),
            "Europe/Berlin",
            (SlotKey(0, SLOT_MS),),
        )
        context = LoadForecastContext(
            0.0,
            (
                LoadDriverSnapshot(
                    "heat_pump_dhw",
                    0.0,
                    features=(LoadFeatureValue("dhw_target_c", 53.0),),
                ),
            ),
        )

        power_w = _component_power_w(
            LoadComponentSpec("heat_pump_dhw", LOAD_PROFILE_HEAT_PUMP, "00:00-05:00"),
            target,
            history,
            context,
            None,
            request,
        )

        self.assertEqual(power_w, 1000.0)

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

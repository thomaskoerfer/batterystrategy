"""Behavior tests for short-horizon cyclic appliance forecasting."""

from __future__ import annotations

import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.component_config import LoadComponentSpec
from custom_components.battery_strategy.const import LOAD_PROFILE_CYCLIC_APPLIANCE
from custom_components.battery_strategy.contracts import (
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadDriverSnapshot,
    LoadFeatureValue,
    LoadForecastContext,
    QualityFlag,
    SlotKey,
)
from custom_components.battery_strategy.forecasting.components import (
    build_component_load_forecast,
)
from custom_components.battery_strategy.forecasting.cyclic_appliance import (
    appliance_cycles,
)
from custom_components.battery_strategy.forecasting.history import ForecastTargetInput
from custom_components.battery_strategy.forecasting.load import LoadForecastModelConfig

SLOT_MS = 900_000
TIMEZONE = ZoneInfo("Europe/Berlin")
COMPONENT_KEY = "washing_machine"
PROFILE = (0.10, 0.20, 0.15, 0.05)


def _history(active_tail: tuple[float, ...] = ()):
    event_starts = (96, 220, 350, 500)
    count = 7 * 96
    energy = [0.0] * count
    for start in event_starts:
        energy[start : start + len(PROFILE)] = PROFILE
    if active_tail:
        energy[-len(active_tail) :] = active_tail
    return tuple(
        HistoricalFeatureSlot(
            slot=SlotKey(index * SLOT_MS, (index + 1) * SLOT_MS),
            house_load_no_ev_kwh=0.20 + component_kwh,
            pv_generation_kwh=0.0,
            grid_import_kwh=0.20 + component_kwh,
            grid_export_kwh=0.0,
            battery_charge_kwh=0.0,
            battery_discharge_kwh=0.0,
            ev_charge_kwh=0.0,
            price_ct_per_kwh=30.0,
            load_components=(
                LoadComponentEnergy(
                    COMPONENT_KEY,
                    component_kwh,
                    features=(
                        LoadFeatureValue(
                            "cycle_active_fraction",
                            1.0 if component_kwh > 0.0 else 0.0,
                        ),
                    ),
                ),
            ),
        )
        for index, component_kwh in enumerate(energy)
    )


def _forecast(history, driver, slot_count=5):
    start = history[-1].slot.end_ms
    slots = tuple(
        SlotKey(start + index * SLOT_MS, start + (index + 1) * SLOT_MS)
        for index in range(slot_count)
    )
    request = ForecastRequest(start, "Europe/Berlin", slots)
    targets = tuple(
        ForecastTargetInput(
            dt.datetime.fromtimestamp(slot.start_ms / 1000.0, dt.UTC).astimezone(
                TIMEZONE
            ),
            1.0,
        )
        for slot in slots
    )
    return build_component_load_forecast(
        request,
        history,
        targets,
        LoadForecastContext(0.0, (driver,)),
        (),
        (LoadComponentSpec(COMPONENT_KEY, LOAD_PROFILE_CYCLIC_APPLIANCE),),
        LoadForecastModelConfig("Europe/Berlin", 1.0, (1.0,) * 96),
    )


def _mixed_length_history(active_tail: tuple[float, ...]):
    profiles = (
        (0.10, 0.20),
        (0.10, 0.20),
        (0.10, 0.20),
        (0.10, 0.20, 0.30, 0.10),
        (0.10, 0.20, 0.30, 0.10),
    )
    count = 7 * 96
    energy = [0.0] * count
    for start, profile in zip((96, 180, 270, 360, 450), profiles, strict=True):
        energy[start : start + len(profile)] = profile
    energy[-len(active_tail) :] = active_tail
    return tuple(
        HistoricalFeatureSlot(
            slot=SlotKey(index * SLOT_MS, (index + 1) * SLOT_MS),
            house_load_no_ev_kwh=0.20 + component_kwh,
            pv_generation_kwh=0.0,
            grid_import_kwh=0.20 + component_kwh,
            grid_export_kwh=0.0,
            battery_charge_kwh=0.0,
            battery_discharge_kwh=0.0,
            ev_charge_kwh=0.0,
            price_ct_per_kwh=30.0,
            load_components=(LoadComponentEnergy(COMPONENT_KEY, component_kwh),),
        )
        for index, component_kwh in enumerate(energy)
    )


class CyclicApplianceForecastTests(unittest.TestCase):
    def test_inactive_appliance_does_not_create_speculative_p50_load(self):
        forecast = _forecast(
            _history(),
            LoadDriverSnapshot(
                COMPONENT_KEY,
                0.0,
                features=(LoadFeatureValue("cycle_active_fraction", 0.0),),
            ),
        )

        appliance = next(
            item for item in forecast.components if item.component_key == COMPONENT_KEY
        )

        self.assertEqual([slot.energy.p50_kwh for slot in appliance.slots], [0.0] * 5)

    def test_explicit_inactive_state_wins_over_residual_meter_power(self):
        forecast = _forecast(
            _history(),
            LoadDriverSnapshot(
                COMPONENT_KEY,
                30.0,
                features=(LoadFeatureValue("cycle_active_fraction", 0.0),),
            ),
        )

        appliance = next(
            item for item in forecast.components if item.component_key == COMPONENT_KEY
        )

        self.assertEqual([slot.energy.p50_kwh for slot in appliance.slots], [0.0] * 5)

    def test_active_appliance_forecasts_remaining_learned_cycle(self):
        forecast = _forecast(
            _history(PROFILE[:2]),
            LoadDriverSnapshot(
                COMPONENT_KEY,
                600.0,
                features=(LoadFeatureValue("cycle_active_fraction", 1.0),),
            ),
        )

        appliance = next(
            item for item in forecast.components if item.component_key == COMPONENT_KEY
        )
        energies = [slot.energy.p50_kwh for slot in appliance.slots]

        self.assertEqual(energies[:2], [0.15, 0.05])
        self.assertEqual(energies[2:], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(
            forecast.slots[0].energy.p50_kwh,
            sum(item.slots[0].energy.p50_kwh for item in forecast.components),
        )

    def test_reported_remaining_time_caps_cycle_tail(self):
        forecast = _forecast(
            _history(PROFILE[:1]),
            LoadDriverSnapshot(
                COMPONENT_KEY,
                800.0,
                features=(
                    LoadFeatureValue("cycle_active_fraction", 1.0),
                    LoadFeatureValue("cycle_remaining_s", 1000.0),
                ),
            ),
        )

        appliance = next(
            item for item in forecast.components if item.component_key == COMPONENT_KEY
        )

        self.assertGreater(appliance.slots[0].energy.p50_kwh, 0.0)
        self.assertGreater(appliance.slots[1].energy.p50_kwh, 0.0)
        self.assertEqual(appliance.slots[2].energy.p50_kwh, 0.0)

    def test_completed_short_cycles_contribute_zero_to_later_slots(self):
        forecast = _forecast(
            _mixed_length_history((0.10,)),
            LoadDriverSnapshot(
                COMPONENT_KEY,
                600.0,
                features=(LoadFeatureValue("cycle_active_fraction", 1.0),),
            ),
        )

        appliance = next(
            item for item in forecast.components if item.component_key == COMPONENT_KEY
        )

        self.assertEqual(
            [slot.energy.p50_kwh for slot in appliance.slots],
            [0.20, 0.0, 0.0, 0.0, 0.0],
        )

    def test_power_only_start_does_not_restart_a_complete_historical_cycle(self):
        forecast = _forecast(
            _history(),
            LoadDriverSnapshot(COMPONENT_KEY, 800.0),
        )

        appliance = next(
            item for item in forecast.components if item.component_key == COMPONENT_KEY
        )

        self.assertEqual(
            [slot.energy.p50_kwh for slot in appliance.slots],
            [0.2, 0.0, 0.0, 0.0, 0.0],
        )
        self.assertEqual(appliance.model_version, "cyclic_appliance-fallback-v1")
        self.assertIn(QualityFlag.ESTIMATED, appliance.slots[0].quality.flags)

    def test_missing_slots_do_not_join_two_cycle_fragments(self):
        history = tuple(
            item for index, item in enumerate(_history()) if index not in (97, 98)
        )

        cycles, _tail = appliance_cycles(history, COMPONENT_KEY)

        assert all(cycle.start_ms != 96 * SLOT_MS for cycle in cycles)

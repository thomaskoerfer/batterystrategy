"""Regression tests for the production feature-store forecast application."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.battery_strategy import planning_pipeline
from custom_components.battery_strategy.contracts import (
    DataQuality,
    HistoricalFeatureSlot,
    LoadForecastContext,
    PvPlant,
    SlotKey,
    WeatherSlot,
)
from custom_components.battery_strategy.forecasting import (
    FeatureStoreForecastNotReady,
    ForecastModelConfig,
    build_feature_store_forecast,
    weather_targets,
)
from custom_components.battery_strategy.planning_state import (
    STATE_SCHEMA_VERSION,
    PlanningStateStore,
)
from custom_components.battery_strategy.runtime_market_data import TariffInterval
from custom_components.battery_strategy.state_document import save_state_document
from tests.planning_runtime_helpers import runtime_snapshot, settings_from_values


class ForecastProductionTests(unittest.TestCase):
    def test_forecasting_package_has_no_runtime_or_actuator_dependency(self):
        package_dir = (
            Path(__file__).parents[1]
            / "custom_components"
            / "battery_strategy"
            / "forecasting"
        )
        allowed_absolute = {
            "__future__",
            "dataclasses",
            "datetime",
            "math",
            "statistics",
            "zoneinfo",
        }
        for path in package_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                    self.assertTrue(roots <= allowed_absolute)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    root = (node.module or "").split(".", 1)[0]
                    self.assertIn(root, allowed_absolute)

    def setUp(self) -> None:
        self.timezone = ZoneInfo("Europe/Berlin")
        self.start = dt.datetime(2026, 8, 16, 10, 0, tzinfo=self.timezone)
        self.intervals = [
            TariffInterval(
                self.start + dt.timedelta(minutes=15 * index),
                0.20 + 0.001 * index,
            )
            for index in range(16)
        ]
        self.history = self._history(8)
        self.runtime = runtime_snapshot(
            captured_at_ms=int(self.start.timestamp() * 1000),
            settings=settings_from_values(timezone="Europe/Berlin"),
        )

    def _history(self, days: int):
        first = self.start.astimezone(dt.UTC) - dt.timedelta(days=days)
        first = first.replace(minute=(first.minute // 15) * 15, second=0, microsecond=0)
        slots = []
        for index in range(days * 96):
            start = first + dt.timedelta(minutes=15 * index)
            slot = SlotKey(
                int(start.timestamp() * 1000),
                int((start + dt.timedelta(minutes=15)).timestamp() * 1000),
            )
            quarter = index % 96
            load_w = 350.0 + 200.0 * (quarter in range(28, 40))
            pv_w = max(0.0, 1000.0 - abs(quarter - 52) * 70.0)
            slots.append(
                HistoricalFeatureSlot(
                    slot=slot,
                    house_load_no_ev_kwh=load_w / 4000.0,
                    pv_generation_kwh=pv_w / 4000.0,
                    grid_import_kwh=max(0.0, load_w - pv_w) / 4000.0,
                    grid_export_kwh=max(0.0, pv_w - load_w) / 4000.0,
                    battery_charge_kwh=0.0,
                    battery_discharge_kwh=0.0,
                    ev_charge_kwh=0.0,
                    price_ct_per_kwh=25.0,
                    quality=DataQuality(),
                )
            )
        return tuple(slots)

    def _forecast(self, history=None, *, context=None, plant=None):
        request = planning_pipeline.forecast_request(
            self.intervals,
            captured_at_ms=int(self.start.timestamp() * 1000),
            timezone="Europe/Berlin",
        )
        result = planning_pipeline.ProductionForecastModule().forecast(
            request,
            self.history if history is None else history,
            context or LoadForecastContext(400.0),
            (),
            plant or PvPlant(2.3, 2.0),
            planning_pipeline.ProductionForecastConfig(
                load_bias=1.0,
                load_slot_biases=(1.0,) * 96,
                pv_global_bias=1.0,
                pv_slot_biases=(1.0,) * 96,
                current_weather_factor=0.8,
                current_pv_w=600.0,
                tomorrow_energy_kwh=None,
            ),
        )
        return result.bundle, result.diagnostics

    def test_feature_store_is_the_only_production_source(self):
        bundle, summary = self._forecast()
        self.assertEqual(summary["source"], "feature_store")
        self.assertEqual(summary["slot_count"], 16)
        self.assertEqual(len(bundle.load.slots), 16)
        self.assertEqual(len(bundle.pv.slots), 16)
        self.assertGreaterEqual(summary["load_usable_slots"], 7 * 96)

    def test_contract_forecasters_are_independent_and_composer_only_combines(self):
        bundle, _ = self._forecast()
        self.assertEqual(len(bundle.load.slots), len(bundle.pv.slots))
        self.assertEqual(
            tuple(slot.slot for slot in bundle.load.slots),
            tuple(slot.slot for slot in bundle.pv.slots),
        )
        changed_plant, _ = self._forecast(plant=PvPlant(0.1, 0.1))
        self.assertEqual(changed_plant.load, bundle.load)
        changed_load, _ = self._forecast(context=LoadForecastContext(1600.0))
        self.assertEqual(changed_load.pv, bundle.pv)

    def test_composed_forecasters_preserve_feature_store_output_exactly(self):
        request = planning_pipeline.forecast_request(
            self.intervals,
            captured_at_ms=int(self.start.timestamp() * 1000),
            timezone="Europe/Berlin",
        )
        config = ForecastModelConfig(
            timezone="Europe/Berlin",
            load_bias=1.0,
            load_slot_biases=(1.0,) * 96,
            pv_global_bias=1.0,
            pv_slot_biases=(1.0,) * 96,
            current_weather_factor=0.8,
            current_pv_w=600.0,
            tomorrow_date=(self.start.date() + dt.timedelta(days=1)).isoformat(),
            tomorrow_energy_kwh=None,
            pv_capacity_kwp=2.3,
            pv_inverter_kw=2.0,
        )
        expected = build_feature_store_forecast(
            request,
            self.history,
            weather_targets(request, (), 0.8),
            LoadForecastContext(400.0),
            config,
        )
        actual, _ = self._forecast()

        self.assertEqual(actual, expected)

    def test_weather_targets_preserve_last_quarter_hourly_alignment(self):
        request = planning_pipeline.forecast_request(
            self.intervals[:4],
            captured_at_ms=int(self.start.timestamp() * 1000),
            timezone="Europe/Berlin",
        )
        weather = tuple(
            WeatherSlot(slot, shortwave_radiation_w_m2=100.0 * (index + 1))
            for index, slot in enumerate(request.slots)
        )

        targets = weather_targets(request, weather, 0.5)

        self.assertEqual(len({target.weather_factor for target in targets}), 1)
        self.assertEqual(
            targets[0].weather_factor,
            planning_pipeline.weather_factor_from_cloud_rad(0.0, 400.0),
        )

    def test_per_run_forecast_observations_are_not_constructor_state(self):
        module_init = inspect.signature(planning_pipeline.ProductionForecastModule)
        self.assertEqual(len(module_init.parameters), 0)
        forecast_parameters = inspect.signature(
            planning_pipeline.ProductionForecastModule.forecast
        ).parameters
        self.assertIn("config", forecast_parameters)

    def test_future_history_is_not_visible_to_forecast(self):
        baseline, _ = self._forecast()
        future_start = self.start.astimezone(dt.UTC) + dt.timedelta(days=1)
        future = HistoricalFeatureSlot(
            slot=SlotKey(
                int(future_start.timestamp() * 1000),
                int((future_start + dt.timedelta(minutes=15)).timestamp() * 1000),
            ),
            house_load_no_ev_kwh=99.0,
            pv_generation_kwh=99.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            battery_charge_kwh=0.0,
            battery_discharge_kwh=0.0,
            ev_charge_kwh=0.0,
            price_ct_per_kwh=0.0,
        )
        candidate, _ = self._forecast((*self.history, future))
        self.assertEqual(candidate, baseline)

    def test_insufficient_feature_history_fails_closed(self):
        with self.assertRaises(FeatureStoreForecastNotReady):
            self._forecast(self._history(2))

    def test_optimizer_requires_explicit_forecast_bundle(self):
        with self.assertRaisesRegex(TypeError, "forecast_bundle"):
            planning_pipeline._planning_service(self.runtime.settings).plan(
                intervals=self.intervals, samples=[], start_energy_kwh=3.0
            )

    def test_optimizer_does_not_construct_forecasts(self):
        source = inspect.getsource(
            planning_pipeline._planning_service(self.runtime.settings).plan
        )
        self.assertNotIn("build_production_forecast", source)
        self.assertNotIn("build_feature_store_forecast", source)

    def test_optimizer_has_no_shadow_evaluation_dependency(self):
        source = Path(planning_pipeline.__file__).read_text(encoding="utf-8")
        self.assertNotIn("shadow_history", source)
        self.assertNotIn("evaluate_feature_store_shadow", source)

    def test_load_state_removes_obsolete_comparison_traces(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "battery_strategy_optimizer_state.json"
            save_state_document(
                state_path,
                {
                    "forecast_shadow_trace": [{"slot_start_ts": 1234}],
                    "forecast_parity_trace": [{"slot_start_ts": 5678}],
                    "state_schema": 7,
                },
            )
            settings = settings_from_values(timezone="Europe/Berlin")
            store = PlanningStateStore(str(state_path))
            state = store.load(settings, int(self.start.timestamp() * 1000))
            document = store.to_document(state)
        self.assertNotIn("forecast_shadow_trace", document)
        self.assertNotIn("forecast_parity_trace", document)
        self.assertEqual(document["state_schema"], STATE_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()

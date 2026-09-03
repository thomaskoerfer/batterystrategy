"""Regression tests for the feature-store production forecast cutover."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from custom_components.battery_strategy import planning_pipeline
from custom_components.battery_strategy.contracts import (
    DataQuality,
    HistoricalFeatureSlot,
    LoadForecastContext,
    SlotKey,
)
from custom_components.battery_strategy.forecasting import FeatureStoreForecastNotReady


class ForecastProductionTests(unittest.TestCase):
    def test_forecasting_package_has_no_runtime_or_actuator_dependency(self):
        package_dir = Path(__file__).parents[1] / "custom_components" / "battery_strategy" / "forecasting"
        allowed_absolute = {"__future__", "dataclasses", "datetime", "math", "statistics", "zoneinfo"}
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
            {"dt": self.start + dt.timedelta(minutes=15 * index), "price_eur": 0.20 + 0.001 * index}
            for index in range(16)
        ]
        self.history = self._history(8)
        self.old_timezone = planning_pipeline.OPEN_METEO_TZ
        planning_pipeline.OPEN_METEO_TZ = self.timezone
        planning_pipeline.local_dt_from_ts.cache_clear()

    def tearDown(self) -> None:
        planning_pipeline.OPEN_METEO_TZ = self.old_timezone
        planning_pipeline.local_dt_from_ts.cache_clear()

    def _history(self, days: int):
        first = self.start.astimezone(dt.timezone.utc) - dt.timedelta(days=days)
        first = first.replace(minute=(first.minute // 15) * 15, second=0, microsecond=0)
        slots = []
        for index in range(days * 96):
            start = first + dt.timedelta(minutes=15 * index)
            slot = SlotKey(int(start.timestamp() * 1000), int((start + dt.timedelta(minutes=15)).timestamp() * 1000))
            quarter = index % 96
            load_w = 350.0 + 200.0 * (quarter in range(28, 40))
            pv_w = max(0.0, 1000.0 - abs(quarter - 52) * 70.0)
            slots.append(HistoricalFeatureSlot(
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
            ))
        return tuple(slots)

    def _forecast(self, history=None):
        targets = planning_pipeline.build_forecast_targets(self.intervals, 0.8)
        return planning_pipeline.build_production_forecast(
            self.intervals,
            targets,
            now_local=self.start,
            weather_factor=0.8,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * 96,
            pv_bias_slots=[1.0] * 96,
            pv_now_actual_w=600.0,
            pv_global_bias=1.0,
            pv_capacity_kwp=2.3,
            pv_inverter_kw=2.0,
            history=self.history if history is None else history,
            context=LoadForecastContext(400.0),
            weather=(),
            component_specs=(),
        )

    def test_feature_store_is_the_only_production_source(self):
        bundle, summary = self._forecast()
        self.assertEqual(summary["source"], "feature_store")
        self.assertEqual(summary["slot_count"], 16)
        self.assertEqual(len(bundle.load.slots), 16)
        self.assertEqual(len(bundle.pv.slots), 16)
        self.assertGreaterEqual(summary["load_usable_slots"], 7 * 96)

    def test_future_history_is_not_visible_to_forecast(self):
        baseline, _ = self._forecast()
        future_start = self.start.astimezone(dt.timezone.utc) + dt.timedelta(days=1)
        future = HistoricalFeatureSlot(
            slot=SlotKey(int(future_start.timestamp() * 1000), int((future_start + dt.timedelta(minutes=15)).timestamp() * 1000)),
            house_load_no_ev_kwh=99.0,
            pv_generation_kwh=99.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            battery_charge_kwh=0.0,
            battery_discharge_kwh=0.0,
            ev_charge_kwh=0.0,
            price_ct_per_kwh=0.0,
        )
        candidate, _ = self._forecast(self.history + (future,))
        self.assertEqual(candidate, baseline)

    def test_insufficient_feature_history_fails_closed(self):
        with self.assertRaises(FeatureStoreForecastNotReady):
            self._forecast(self._history(2))

    def test_optimizer_requires_explicit_forecast_bundle(self):
        with self.assertRaisesRegex(TypeError, "forecast_bundle"):
            planning_pipeline._planning_service().plan(
                intervals=self.intervals, samples=[], start_energy_kwh=3.0
            )

    def test_optimizer_does_not_construct_forecasts(self):
        source = inspect.getsource(planning_pipeline._planning_service().plan)
        self.assertNotIn("build_production_forecast", source)
        self.assertNotIn("build_feature_store_forecast", source)

    def test_optimizer_has_no_shadow_evaluation_dependency(self):
        source = Path(planning_pipeline.__file__).read_text(encoding="utf-8")
        self.assertNotIn("shadow_history", source)
        self.assertNotIn("evaluate_feature_store_shadow", source)

    def test_load_state_removes_obsolete_comparison_traces(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "optimizer_state.json"
            planning_pipeline.save_state_document(state_path, {
                "forecast_shadow_trace": [{"slot_start_ts": 1234}],
                "forecast_parity_trace": [{"slot_start_ts": 5678}],
                "state_schema": 7,
            })
            with patch.object(planning_pipeline, "STATE_FILE", str(state_path)):
                data = planning_pipeline.load_state()
        self.assertNotIn("forecast_shadow_trace", data)
        self.assertNotIn("forecast_parity_trace", data)
        self.assertEqual(data["state_schema"], 8)


if __name__ == "__main__":
    unittest.main()

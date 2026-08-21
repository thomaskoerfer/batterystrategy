"""Regression tests for the sole extracted forecast production path."""

from __future__ import annotations

import ast
import datetime as dt
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from custom_components.battery_strategy import optimizer_engine


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
            {
                "dt": self.start + dt.timedelta(minutes=15 * index),
                "price_eur": 0.20 + 0.001 * index,
            }
            for index in range(16)
        ]
        self.samples = []
        for days_ago in range(1, 22):
            for index in range(16):
                timestamp = (
                    self.start
                    - dt.timedelta(days=days_ago)
                    + dt.timedelta(minutes=15 * index)
                )
                self.samples.append(
                    {
                        "ts": timestamp.timestamp(),
                        "load_w": 400.0 + 10.0 * index,
                        "pv_w": max(0.0, 800.0 - 30.0 * abs(index - 8)),
                        "grid_import_w": 100.0,
                        "grid_export_w": 0.0,
                        "hp_w": 700.0,
                    }
                )
        self.old_timezone = optimizer_engine.OPEN_METEO_TZ
        self.old_capacity_events = optimizer_engine.PV_CAPACITY_EVENTS
        optimizer_engine.OPEN_METEO_TZ = self.timezone
        optimizer_engine.PV_CAPACITY_EVENTS = [("2000-01-01T00:00:00+00:00", 2.3, 2.0)]
        optimizer_engine.local_dt_from_ts.cache_clear()

    def tearDown(self) -> None:
        optimizer_engine.OPEN_METEO_TZ = self.old_timezone
        optimizer_engine.PV_CAPACITY_EVENTS = self.old_capacity_events
        optimizer_engine.local_dt_from_ts.cache_clear()

    def _plan(self, **changes):
        return optimizer_engine.build_virtual_plan(
            self.intervals,
            self.samples,
            start_energy_kwh=3.0,
            weather_factor=0.8,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * 96,
            pv_bias_slots=[1.0] * 96,
            now_local=self.start,
            pv_now_actual_w=600.0,
            pv_global_bias=1.0,
            **changes,
        )

    def test_extracted_forecast_is_the_only_plan_input(self):
        plan = self._plan()
        summary = plan["forecast_diagnostics"]
        self.assertEqual(summary["source"], "extracted")
        self.assertEqual(summary["slot_count"], 16)
        self.assertEqual(plan["points"][0]["load_fc_w"], 455.4)
        self.assertEqual(plan["points"][0]["pv_fc_w"], 600.0)

    def test_extracted_forecast_change_is_authoritative(self):
        original = optimizer_engine.build_legacy_forecast

        def shifted_forecast(*args, **kwargs):
            bundle = original(*args, **kwargs)
            first = bundle.load.slots[0]
            shifted_first = replace(
                first,
                energy=replace(
                    first.energy,
                    p50_kwh=first.energy.p50_kwh
                    + 2.0 * optimizer_engine.SLOT_H / 1000.0,
                ),
            )
            shifted_component = replace(
                bundle.load.components[0],
                slots=(shifted_first, *bundle.load.components[0].slots[1:]),
            )
            return replace(
                bundle,
                load=replace(
                    bundle.load,
                    slots=(shifted_first, *bundle.load.slots[1:]),
                    components=(shifted_component,),
                ),
            )

        with patch.object(
            optimizer_engine, "build_legacy_forecast", side_effect=shifted_forecast
        ):
            plan = self._plan()
        self.assertEqual(plan["points"][0]["load_fc_w"], 457.4)

    def test_extracted_failure_propagates_to_global_fail_safe(self):
        with patch.object(
            optimizer_engine,
            "build_legacy_forecast",
            side_effect=RuntimeError("production forecast test failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "production forecast test failure"
            ):
                self._plan()

    def test_shadow_failure_does_not_change_production_plan(self):
        baseline = self._plan()
        with patch.object(
            optimizer_engine,
            "evaluate_feature_store_shadow",
            side_effect=RuntimeError("isolated shadow failure"),
        ):
            shadowed = self._plan(shadow_history=(object(),))
        self.assertEqual(baseline["points"], shadowed["points"])
        comparison = shadowed["forecast_diagnostics"]["shadow_comparison"]
        self.assertEqual(comparison["status"], "error")
        self.assertIn("isolated shadow failure", comparison["reason"])

    def test_load_state_removes_obsolete_comparison_traces(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "optimizer_state.json"
            optimizer_engine.save_state_document(
                state_path,
                {
                    "forecast_shadow_trace": [{"slot_start_ts": 1234}],
                    "forecast_parity_trace": [{"slot_start_ts": 5678}],
                    "state_schema": 7,
                },
            )
            with patch.object(optimizer_engine, "STATE_FILE", str(state_path)):
                data = optimizer_engine.load_state()

        self.assertNotIn("forecast_shadow_trace", data)
        self.assertNotIn("forecast_parity_trace", data)
        self.assertEqual(data["state_schema"], 8)


if __name__ == "__main__":
    unittest.main()

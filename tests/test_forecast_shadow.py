"""Tests for isolated recorder-independent shadow forecasting."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from custom_components.battery_strategy.contracts import (
    DataQuality,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadForecastContext,
    SlotKey,
)
from custom_components.battery_strategy.forecast_shadow_runner import (
    ForecastShadowRunner,
)
from custom_components.battery_strategy.forecast_shadow_store import (
    ForecastShadowTraceStore,
)
from custom_components.battery_strategy.forecasting import (
    LegacyForecastConfig,
    LegacyForecastSample,
    LegacyForecastTarget,
    build_legacy_forecast,
    build_legacy_load_forecast,
    build_legacy_pv_forecast,
    evaluate_feature_store_shadow,
)

SLOT_MS = 15 * 60 * 1000


def feature(start_ms: int, load_kwh: float = 0.1, pv_kwh: float = 0.05):
    return HistoricalFeatureSlot(
        slot=SlotKey(start_ms, start_ms + SLOT_MS),
        house_load_no_ev_kwh=load_kwh,
        pv_generation_kwh=pv_kwh,
        grid_import_kwh=max(0.0, load_kwh - pv_kwh),
        grid_export_kwh=max(0.0, pv_kwh - load_kwh),
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        ev_charge_kwh=0.0,
        price_ct_per_kwh=30.0,
        quality=DataQuality(),
    )


class ForecastShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of_ms = (7 * 96 + 48) * SLOT_MS
        self.history = tuple(feature(index * SLOT_MS) for index in range(7 * 96 + 48))
        self.request = ForecastRequest(
            self.as_of_ms,
            "UTC",
            tuple(
                SlotKey(
                    self.as_of_ms + index * SLOT_MS,
                    self.as_of_ms + (index + 1) * SLOT_MS,
                )
                for index in range(4)
            ),
        )
        self.targets = tuple(
            LegacyForecastTarget(
                dt.datetime.fromtimestamp(item.start_ms / 1000, tz=dt.timezone.utc),
                1.0,
            )
            for item in self.request.slots
        )
        self.samples = tuple(
            LegacyForecastSample(
                item.slot.start_ms / 1000,
                item.house_load_no_ev_kwh * 4000,
                item.pv_generation_kwh * 4000,
                item.grid_import_kwh * 4000,
                item.grid_export_kwh * 4000,
            )
            for item in self.history
        )
        self.context = LoadForecastContext(400.0)
        self.config = LegacyForecastConfig(
            timezone="UTC",
            load_bias=1.0,
            load_slot_biases=(1.0,) * 96,
            pv_global_bias=1.0,
            pv_slot_biases=(1.0,) * 96,
            current_weather_factor=1.0,
            current_pv_w=None,
            tomorrow_date="2099-01-01",
            tomorrow_energy_kwh=None,
            pv_capacity_kwp=2.3,
            pv_inverter_kw=2.0,
        )

    def test_load_and_pv_modules_do_not_import_each_other(self):
        package = (
            Path(__file__).parents[1]
            / "custom_components"
            / "battery_strategy"
            / "forecasting"
        )
        self.assertNotIn("from .pv import", (package / "load.py").read_text())
        self.assertNotIn("from .load import", (package / "pv.py").read_text())

    def test_load_and_pv_modules_are_independently_configured(self):
        load = build_legacy_load_forecast(
            self.request,
            self.samples,
            self.targets,
            self.context,
            self.config.load_config(),
        )
        pv = build_legacy_pv_forecast(
            self.request,
            self.samples,
            self.targets,
            self.config.pv_config(),
        )
        changed = replace(self.config, pv_global_bias=0.5)
        unchanged_load = build_legacy_load_forecast(
            self.request,
            self.samples,
            self.targets,
            self.context,
            changed.load_config(),
        )
        changed_pv = build_legacy_pv_forecast(
            self.request,
            self.samples,
            self.targets,
            changed.pv_config(),
        )
        self.assertEqual(load.slots, unchanged_load.slots)
        self.assertNotEqual(pv.slots, changed_pv.slots)
        self.assertEqual(load.components[0].component_key, "general_house_load")

    def test_shadow_excludes_future_history_and_becomes_ready_after_seven_days(self):
        production = build_legacy_forecast(
            self.request, self.samples, self.targets, self.context, self.config
        )
        future = feature(self.as_of_ms + 10 * SLOT_MS, load_kwh=9.0, pv_kwh=9.0)
        comparison = evaluate_feature_store_shadow(
            production=production,
            request=self.request,
            history=(*self.history, future),
            targets=self.targets,
            context=self.context,
            config=self.config,
        )
        self.assertEqual(comparison.status, "ready")
        self.assertEqual(comparison.history_slot_count, 7 * 96 + 48)
        self.assertFalse(comparison.authoritative)
        self.assertEqual(
            tuple(point.lead_minutes for point in comparison.points),
            (15, 60),
        )

    def test_trace_is_deduplicated_and_matures_against_actual_slot(self):
        production = build_legacy_forecast(
            self.request, self.samples, self.targets, self.context, self.config
        )
        comparison = evaluate_feature_store_shadow(
            production=production,
            request=self.request,
            history=self.history,
            targets=self.targets,
            context=self.context,
            config=self.config,
        )
        point = comparison.points[0]
        actual = feature(
            point.target.start_ms,
            load_kwh=point.shadow_load_kwh,
            pv_kwh=point.shadow_pv_kwh,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ForecastShadowTraceStore(Path(temp_dir) / "shadow.json.gz")
            first = store.record(comparison, (actual,))
            second = store.record(comparison, (actual,))
        self.assertEqual(first["trace_count"], 1)
        self.assertEqual(second["trace_count"], 1)
        self.assertEqual(second["matured_count"], 1)
        self.assertEqual(second["shadow_load_mae_kwh"], 0.0)
        self.assertEqual(second["shadow_pv_mae_kwh"], 0.0)

    def test_trace_retention_is_bounded_to_fourteen_days(self):
        production = build_legacy_forecast(
            self.request, self.samples, self.targets, self.context, self.config
        )
        base = evaluate_feature_store_shadow(
            production=production,
            request=self.request,
            history=self.history,
            targets=self.targets,
            context=self.context,
            config=self.config,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ForecastShadowTraceStore(Path(temp_dir) / "shadow.json.gz")
            store.record(replace(base, generated_at_ms=1), ())
            diagnostics = store.record(replace(base, generated_at_ms=15 * 86_400_000), ())
        self.assertEqual(diagnostics["trace_count"], 1)

    def test_runner_failure_remains_non_authoritative_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = ForecastShadowRunner(Path(temp_dir) / "shadow.json.gz")
            diagnostics = runner.evaluate({"invalid": True})
        self.assertEqual(diagnostics["status"], "error")
        self.assertFalse(diagnostics["authoritative"])


if __name__ == "__main__":
    unittest.main()

"""Tests for the recorder-independent production feature store."""

from __future__ import annotations

import datetime as dt
import gzip
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from custom_components.battery_strategy.contracts import QualityFlag
from custom_components.battery_strategy.coordinator import BatteryStrategyCoordinator
from custom_components.battery_strategy.feature_store import (
    CompressedFeatureStore,
    ExecutorFeatureStore,
    FeatureAggregator,
    FeatureObservation,
)

SLOT_MS = 15 * 60 * 1000


def observation(timestamp_ms: int, **changes) -> FeatureObservation:
    values = {
        "grid_import_w": 500.0,
        "grid_export_w": 0.0,
        "pv_generation_w": 300.0,
        "battery_power_w": 100.0,
        "ev_charge_w": 200.0,
        "price_ct_per_kwh": 30.0,
        "quality_flags": (),
    }
    values.update(changes)
    return FeatureObservation(timestamp_ms=timestamp_ms, **values)


def complete_slot(aggregator: FeatureAggregator, **changes):
    """Feed one slot with realistic minute-level observations."""
    aggregator.observe(observation(0, **changes))
    finalized = ()
    for timestamp_ms in range(60_000, SLOT_MS + 1, 60_000):
        finalized = aggregator.observe(observation(timestamp_ms, **changes))
    return finalized[0]


class FeatureAggregatorTests(unittest.TestCase):
    def test_time_weighted_slot_reconstructs_ev_free_house_and_battery_flows(self):
        aggregator = FeatureAggregator()
        slot = complete_slot(aggregator)
        # Total house load: 500 + 300 + 100 = 900 W; EV-free load: 700 W.
        self.assertAlmostEqual(slot.house_load_no_ev_kwh, 0.175)
        self.assertAlmostEqual(slot.pv_generation_kwh, 0.075)
        self.assertAlmostEqual(slot.grid_import_kwh, 0.125)
        self.assertAlmostEqual(slot.battery_discharge_kwh, 0.025)
        self.assertAlmostEqual(slot.battery_charge_kwh, 0.0)
        self.assertAlmostEqual(slot.ev_charge_kwh, 0.05)
        self.assertAlmostEqual(slot.price_ct_per_kwh, 30.0)
        self.assertEqual(slot.quality.coverage, 1.0)
        self.assertNotIn(QualityFlag.RESTART_GAP, slot.quality.flags)

    def test_charge_sign_and_export_reconstruct_house_load(self):
        aggregator = FeatureAggregator()
        slot = complete_slot(
            aggregator,
            grid_import_w=0.0,
            grid_export_w=100.0,
            pv_generation_w=800.0,
            battery_power_w=-200.0,
            ev_charge_w=0.0,
        )
        self.assertAlmostEqual(slot.house_load_no_ev_kwh, 0.125)
        self.assertAlmostEqual(slot.battery_charge_kwh, 0.05)
        self.assertAlmostEqual(slot.grid_export_kwh, 0.025)

    def test_named_load_components_are_aggregated_separately(self):
        aggregator = FeatureAggregator()
        slot = complete_slot(
            aggregator,
            load_components_w=(("heat_pump", 100.0),),
        )
        self.assertAlmostEqual(slot.load_components[0].energy_kwh, 0.025)
        self.assertEqual(slot.load_components[0].component_key, "heat_pump")
        self.assertEqual(slot.load_components[0].quality.coverage, 1.0)
        self.assertGreater(
            slot.house_load_no_ev_kwh, slot.load_components[0].energy_kwh
        )

    def test_component_context_features_are_time_weighted_and_persisted(self):
        aggregator = FeatureAggregator()
        slot = complete_slot(
            aggregator,
            load_components_w=(("heat_pump_dhw", 100.0),),
            load_component_features=(
                ("heat_pump_dhw", (("dhw_temperature_c", 45.0),)),
            ),
        )
        self.assertEqual(
            slot.load_components[0].features[0].feature_key,
            "dhw_temperature_c",
        )
        self.assertAlmostEqual(slot.load_components[0].features[0].value, 45.0)
        self.assertEqual(slot.load_components[0].features[0].quality.coverage, 1.0)

    def test_component_meter_mismatch_is_flagged_instead_of_rejected(self):
        aggregator = FeatureAggregator()
        slot = complete_slot(
            aggregator,
            load_components_w=(("heat_pump", 2_000.0),),
        )
        self.assertIn(
            QualityFlag.COMPONENT_MISMATCH,
            slot.load_components[0].quality.flags,
        )

    def test_long_gap_is_not_integrated_and_is_quality_flagged(self):
        aggregator = FeatureAggregator()
        aggregator.observe(observation(0))
        slot = aggregator.observe(observation(SLOT_MS))[0]
        self.assertEqual(slot.quality.coverage, 0.0)
        self.assertIn(QualityFlag.RESTART_GAP, slot.quality.flags)
        self.assertIn(QualityFlag.ESTIMATED, slot.quality.flags)

    def test_missing_inputs_and_price_are_retained_as_quality_metadata(self):
        aggregator = FeatureAggregator()
        sample = observation(
            0,
            price_ct_per_kwh=None,
            quality_flags=(QualityFlag.MISSING_PV, QualityFlag.MISSING_EV),
        )
        aggregator.observe(sample)
        # Keep the interval below the gap threshold while completing the slot.
        finalized = ()
        for timestamp_ms in range(60_000, SLOT_MS + 1, 60_000):
            finalized = aggregator.observe(
                observation(
                    timestamp_ms,
                    price_ct_per_kwh=None,
                    quality_flags=(QualityFlag.MISSING_PV, QualityFlag.MISSING_EV),
                )
            )
        slot = finalized[0]
        self.assertIsNone(slot.price_ct_per_kwh)
        self.assertIn(QualityFlag.MISSING_PRICE, slot.quality.flags)
        self.assertIn(QualityFlag.MISSING_PV, slot.quality.flags)
        self.assertIn(QualityFlag.MISSING_EV, slot.quality.flags)


class CompressedFeatureStoreTests(unittest.TestCase):
    def test_executor_adapter_implements_async_feature_store_contract(self):
        self.assertTrue(inspect.iscoroutinefunction(ExecutorFeatureStore.load))
        self.assertTrue(inspect.iscoroutinefunction(ExecutorFeatureStore.upsert))

    def test_store_is_atomic_versioned_deduplicated_and_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "features.json.gz"
            store = CompressedFeatureStore(path, retention_days=1)
            store.initialize()
            aggregator = FeatureAggregator()
            aggregator.observe(observation(0))
            first = aggregator.observe(observation(SLOT_MS))[0]

            late = FeatureObservation(
                timestamp_ms=2 * 86_400_000,
                grid_import_w=100.0,
                grid_export_w=0.0,
                pv_generation_w=0.0,
                battery_power_w=0.0,
                ev_charge_w=0.0,
                price_ct_per_kwh=20.0,
            )
            late_aggregator = FeatureAggregator()
            late_aggregator.observe(late)
            second = late_aggregator.observe(
                FeatureObservation(
                    timestamp_ms=2 * 86_400_000 + SLOT_MS,
                    grid_import_w=100.0,
                    grid_export_w=0.0,
                    pv_generation_w=0.0,
                    battery_power_w=0.0,
                    ev_charge_w=0.0,
                    price_ct_per_kwh=20.0,
                )
            )[0]
            store.upsert((first, first, second))

            self.assertEqual(store.load(0, 3 * 86_400_000), (second,))
            envelope = json.loads(gzip.decompress(path.read_bytes()))
            self.assertEqual(envelope["schema_version"], 3)
            self.assertEqual(len(envelope["slots"]), 1)

            reloaded = CompressedFeatureStore(path, retention_days=1)
            reloaded.initialize()
            self.assertEqual(reloaded.load(0, 3 * 86_400_000), (second,))
            self.assertTrue(reloaded.diagnostics()["authoritative"])

    def test_version_one_store_is_fully_migrated_and_can_be_downgraded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "features.json.gz"
            payload = {
                "schema_version": 1,
                "retention_days": 180,
                "slots": [
                    {
                        "start_ms": 0,
                        "house_load_no_ev_kwh": 0.1,
                        "pv_generation_kwh": 0.0,
                        "grid_import_kwh": 0.1,
                        "grid_export_kwh": 0.0,
                        "battery_charge_kwh": 0.0,
                        "battery_discharge_kwh": 0.0,
                        "ev_charge_kwh": 0.0,
                        "price_ct_per_kwh": 30.0,
                        "coverage": 1.0,
                        "flags": [],
                    }
                ],
            }
            path.write_bytes(gzip.compress(json.dumps(payload).encode()))
            store = CompressedFeatureStore(path)
            store.initialize()
            loaded = store.load(0, SLOT_MS)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].load_components, ())
            upgraded = json.loads(gzip.decompress(path.read_bytes()))
            self.assertEqual(upgraded["schema_version"], 3)
            self.assertTrue(Path(str(path) + ".schema1.bak").exists())
            store.downgrade_to_schema_one()
            downgraded = json.loads(gzip.decompress(path.read_bytes()))
            self.assertEqual(downgraded["schema_version"], 1)
            self.assertNotIn("load_components", downgraded["slots"][0])

    def test_version_two_store_migrates_and_can_be_downgraded_without_energy_loss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "features.json.gz"
            payload = {
                "schema_version": 2,
                "retention_days": 180,
                "slots": [
                    {
                        "start_ms": 0,
                        "house_load_no_ev_kwh": 0.2,
                        "pv_generation_kwh": 0.0,
                        "grid_import_kwh": 0.2,
                        "grid_export_kwh": 0.0,
                        "battery_charge_kwh": 0.0,
                        "battery_discharge_kwh": 0.0,
                        "ev_charge_kwh": 0.0,
                        "price_ct_per_kwh": 30.0,
                        "coverage": 1.0,
                        "flags": [],
                        "load_components": [
                            {
                                "key": "air_conditioning",
                                "energy_kwh": 0.05,
                                "coverage": 1.0,
                                "flags": [],
                            }
                        ],
                    }
                ],
            }
            path.write_bytes(gzip.compress(json.dumps(payload).encode()))
            store = CompressedFeatureStore(path)
            store.initialize()
            self.assertEqual(
                store.load(0, SLOT_MS)[0].load_components[0].energy_kwh, 0.05
            )
            self.assertTrue(Path(str(path) + ".schema2.bak").exists())
            store.downgrade_to_schema_two()
            downgraded = json.loads(gzip.decompress(path.read_bytes()))
            self.assertEqual(downgraded["schema_version"], 2)
            self.assertNotIn("features", downgraded["slots"][0]["load_components"][0])


class FeatureCoordinatorAdapterTests(unittest.TestCase):
    def test_tibber_interval_price_is_normalized_to_ct_per_kwh(self):
        now = dt.datetime(2026, 8, 20, 12, 5, tzinfo=dt.UTC)
        state = SimpleNamespace(
            state="0.31",
            attributes={
                "data": [
                    {
                        "start_time": "2026-08-20T12:00:00+00:00",
                        "price_per_kwh": 0.314,
                    }
                ]
            },
        )
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator.entry = SimpleNamespace(data={"price_entity": "sensor.price"})
        coordinator.hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda entity_id: state)
        )
        self.assertAlmostEqual(coordinator._current_price_ct(now), 31.4)


if __name__ == "__main__":
    unittest.main()

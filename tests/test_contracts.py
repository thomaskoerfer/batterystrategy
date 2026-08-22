"""Executable boundary tests for the target Battery Strategy architecture."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from pathlib import Path

from custom_components.battery_strategy.contracts import (
    BatteryCommand,
    BatteryConstraints,
    BatteryPlan,
    BatteryPlanSlot,
    BatteryState,
    CommandMode,
    CommercialPolicy,
    DataQuality,
    ForecastBundle,
    ForecastRequest,
    ForecastSlot,
    LoadDriverSnapshot,
    LoadFeatureValue,
    LoadForecast,
    LoadForecastComponent,
    LoadForecastContext,
    MarketSlot,
    OptimizationProblem,
    PlanLiveDirective,
    PlanMode,
    PvForecast,
    QualityFlag,
    QuantileEnergy,
    SlotKey,
)

SLOT_MS = 15 * 60 * 1000


def slot(index: int) -> SlotKey:
    """Return one aligned test slot."""
    start = index * SLOT_MS
    return SlotKey(start, start + SLOT_MS)


def forecast_bundle(*slots: SlotKey, generated_at_ms: int = 0) -> ForecastBundle:
    """Return aligned deterministic load and PV forecasts."""
    load_slots = tuple(
        ForecastSlot(item, QuantileEnergy(0.2, 0.1, 0.3, 20)) for item in slots
    )
    pv_slots = tuple(
        ForecastSlot(item, QuantileEnergy(0.1, 0.0, 0.2, 20)) for item in slots
    )
    return ForecastBundle(
        load=LoadForecast(
            "load-1", generated_at_ms, generated_at_ms, "load-v1", load_slots
        ),
        pv=PvForecast("pv-1", generated_at_ms, generated_at_ms, "pv-v1", pv_slots),
    )


class ContractTests(unittest.TestCase):
    def test_contract_package_has_only_standard_library_and_relative_imports(self):
        contract_dir = (
            Path(__file__).parents[1]
            / "custom_components"
            / "battery_strategy"
            / "contracts"
        )
        allowed = {"__future__", "dataclasses", "enum", "math", "typing"}
        for path in contract_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                    self.assertTrue(
                        roots <= allowed, f"forbidden import in {path}: {roots}"
                    )
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    root = (node.module or "").split(".", 1)[0]
                    self.assertIn(root, allowed, f"forbidden import in {path}: {root}")

    def test_slot_requires_aligned_quarter_hour(self):
        with self.assertRaisesRegex(ValueError, "align"):
            SlotKey(1, SLOT_MS + 1)
        with self.assertRaisesRegex(ValueError, "exactly"):
            SlotKey(0, SLOT_MS - 1)

    def test_quality_rejects_invalid_coverage_and_duplicate_flags(self):
        with self.assertRaisesRegex(ValueError, "coverage"):
            DataQuality(1.1)
        with self.assertRaisesRegex(ValueError, "unique"):
            DataQuality(0.8, (QualityFlag.ESTIMATED, QualityFlag.ESTIMATED))

    def test_forecast_request_requires_sorted_nonempty_grid(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            ForecastRequest(0, "Europe/Berlin", ())
        with self.assertRaisesRegex(ValueError, "sorted"):
            ForecastRequest(0, "Europe/Berlin", (slot(1), slot(0)))
        with self.assertRaisesRegex(ValueError, "contiguous"):
            ForecastRequest(0, "Europe/Berlin", (slot(0), slot(2)))

    def test_forecast_quantiles_are_ordered(self):
        with self.assertRaisesRegex(ValueError, "p10 <= p50 <= p90"):
            QuantileEnergy(0.1, 0.2, 0.3, 20)

    def test_forecast_point_is_valid_before_quantile_calibration(self):
        point = QuantileEnergy(0.2)
        self.assertEqual(point.p50_kwh, 0.2)
        self.assertIsNone(point.p10_kwh)
        self.assertIsNone(point.p90_kwh)
        with self.assertRaisesRegex(ValueError, "both be present"):
            QuantileEnergy(0.2, p10_kwh=0.1)
        with self.assertRaisesRegex(ValueError, "require calibration samples"):
            QuantileEnergy(0.2, 0.1, 0.3)

    def test_load_context_supports_unique_extensible_drivers(self):
        context = LoadForecastContext(
            500.0,
            (LoadDriverSnapshot("heat_pump", 300.0),),
        )
        self.assertEqual(context.drivers[0].driver_key, "heat_pump")
        with self.assertRaisesRegex(ValueError, "unique"):
            LoadForecastContext(
                500.0,
                (
                    LoadDriverSnapshot("heat_pump", 300.0),
                    LoadDriverSnapshot("heat_pump", 400.0),
                ),
            )

    def test_load_features_are_finite_and_unique(self):
        driver = LoadDriverSnapshot(
            "heat_pump_dhw",
            0.0,
            features=(LoadFeatureValue("dhw_temperature_c", 45.0),),
        )
        self.assertEqual(driver.features[0].value, 45.0)
        with self.assertRaisesRegex(ValueError, "unique"):
            LoadDriverSnapshot(
                "heat_pump_dhw",
                0.0,
                features=(
                    LoadFeatureValue("dhw_temperature_c", 45.0),
                    LoadFeatureValue("dhw_temperature_c", 46.0),
                ),
            )

    def test_load_components_are_explicit_and_must_sum_to_total(self):
        total = (ForecastSlot(slot(0), QuantileEnergy(0.3)),)
        general = LoadForecastComponent(
            "general_house_load",
            "general-v1",
            0,
            (ForecastSlot(slot(0), QuantileEnergy(0.2)),),
        )
        heat_pump = LoadForecastComponent(
            "heat_pump",
            "heat-pump-v1",
            0,
            (ForecastSlot(slot(0), QuantileEnergy(0.1)),),
        )
        forecast = LoadForecast(
            "load-1",
            0,
            0,
            "composite-v1",
            total,
            (general, heat_pump),
        )
        self.assertEqual(len(forecast.components), 2)
        with self.assertRaisesRegex(ValueError, "sum to total"):
            LoadForecast(
                "load-2",
                0,
                0,
                "composite-v1",
                total,
                (general,),
            )
        with self.assertRaisesRegex(ValueError, "future"):
            LoadForecast(
                "load-3",
                0,
                0,
                "composite-v1",
                total,
                (replace(general, training_cutoff_ms=1, slots=total),),
            )

    def test_forecast_bundle_rejects_misaligned_load_and_pv(self):
        load = LoadForecast(
            "load-1",
            0,
            0,
            "v1",
            (ForecastSlot(slot(0), QuantileEnergy(0.2)),),
        )
        pv = PvForecast(
            "pv-1",
            0,
            0,
            "v1",
            (ForecastSlot(slot(1), QuantileEnergy(0.1)),),
        )
        with self.assertRaisesRegex(ValueError, "same slot grid"):
            ForecastBundle(load, pv)

    def test_optimization_problem_requires_identical_market_grid(self):
        bundle = forecast_bundle(slot(0))
        with self.assertRaisesRegex(ValueError, "same slot grid"):
            OptimizationProblem(
                problem_id="problem-1",
                as_of_ms=0,
                forecast=bundle,
                market=(MarketSlot(slot(1), 30.0),),
                battery=BatteryState(0, 50.0),
                constraints=BatteryConstraints(6.0, 5.0, 100.0, 2400, 2400, 0.8),
                policy=CommercialPolicy(2.0),
            )

    def test_market_prices_and_plan_costs_may_be_negative(self):
        market = MarketSlot(slot(0), -2.5, -1.0)
        plan_slot = BatteryPlanSlot(
            slot(0), PlanMode.IDLE, True, False, 0.0, 0.0, 0.0, 0.0, 50.0, 50.0
        )
        plan = BatteryPlan(
            "plan-1",
            "problem-1",
            0,
            "optimizer-v1",
            BatteryConstraints(6.0, 5.0, 100.0, 2400, 2400, 0.8),
            (plan_slot,),
            -0.2,
            -0.3,
        )
        self.assertEqual(market.import_price_ct_per_kwh, -2.5)
        self.assertEqual(plan.optimized_cost_eur, -0.3)

    def test_optimizer_plan_slot_rejects_simultaneous_flows(self):
        with self.assertRaisesRegex(ValueError, "simultaneously"):
            BatteryPlanSlot(
                slot(0),
                PlanMode.CHARGE,
                True,
                True,
                planned_charge_kwh=0.2,
                planned_discharge_kwh=0.1,
                required_charge_kwh=0.1,
                discharge_budget_kwh=0.0,
                expected_soc_start_pct=50.0,
                expected_soc_end_pct=52.0,
            )

    def test_optimizer_plan_slot_requires_budget_for_planned_discharge(self):
        with self.assertRaisesRegex(ValueError, "commercial budget"):
            BatteryPlanSlot(
                slot(0),
                PlanMode.DISCHARGE,
                True,
                False,
                planned_charge_kwh=0.0,
                planned_discharge_kwh=0.1,
                required_charge_kwh=0.0,
                discharge_budget_kwh=0.05,
                expected_soc_start_pct=50.0,
                expected_soc_end_pct=48.0,
            )

    def test_directive_rejects_power_without_permission(self):
        with self.assertRaisesRegex(ValueError, "grid charge power"):
            PlanLiveDirective(
                directive_id="directive-1",
                plan_id="plan-1",
                issued_at_ms=0,
                slot=slot(0),
                pv_charge_allowed=True,
                grid_charge_allowed=False,
                required_charge_remaining_kwh=0.0,
                max_pv_charge_power_w=1000.0,
                max_grid_charge_power_w=100.0,
                max_discharge_power_w=2400.0,
                discharge_budget_remaining_kwh=0.0,
                min_soc_pct=5.0,
                max_soc_pct=100.0,
            )

    def test_command_fails_closed_for_inconsistent_mode_and_power(self):
        with self.assertRaisesRegex(ValueError, "idle commands"):
            BatteryCommand(
                command_id="command-1",
                directive_id="directive-1",
                created_at_ms=0,
                valid_until_ms=10_000,
                mode=CommandMode.IDLE,
                power_w=100.0,
                reason="invalid_test",
            )
        with self.assertRaisesRegex(ValueError, "active commands"):
            BatteryCommand(
                command_id="command-2",
                directive_id="directive-1",
                created_at_ms=0,
                valid_until_ms=10_000,
                mode=CommandMode.OUTPUT,
                power_w=0.0,
                reason="invalid_test",
            )


if __name__ == "__main__":
    unittest.main()

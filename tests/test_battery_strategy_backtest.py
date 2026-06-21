import importlib.util
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "scripts" / "battery_strategy_backtest.py"
spec = importlib.util.spec_from_file_location("battery_strategy_backtest", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class BatteryStrategyBacktestTests(unittest.TestCase):
    def test_aggregation_excludes_ev_from_dischargeable_load(self):
        samples = [
            mod.RawSample(
                ts=1_800_000.0,
                grid_import_w=5000.0,
                grid_export_w=0.0,
                battery_power_w=0.0,
                pv_w=0.0,
                ev_power_w=4200.0,
                soc_pct=50.0,
                mode="idle",
                power_w=0.0,
                reason="live_idle",
            ),
            mod.RawSample(
                ts=1_800_030.0,
                grid_import_w=5000.0,
                grid_export_w=0.0,
                battery_power_w=0.0,
                pv_w=0.0,
                ev_power_w=4200.0,
                soc_pct=50.0,
                mode="idle",
                power_w=0.0,
                reason="live_idle",
            ),
        ]
        slots = mod.aggregate_slots(samples, [(1_799_000.0, 30.0)], 1_800_000.0, 1_800_030.0)
        self.assertAlmostEqual(slots[0].residual_with_ev_kwh, 5.0 / 120.0, places=4)
        self.assertAlmostEqual(slots[0].dischargeable_load_kwh, 0.8 / 120.0, places=4)

    def test_perfect_foresight_uses_pv_surplus_before_later_load(self):
        slots = [
            mod.Slot(
                ts=1_800_000,
                price_ct=10.0,
                residual_with_ev_kwh=-0.6,
                dischargeable_load_kwh=0.0,
                pv_surplus_kwh=0.6,
                actual_grid_import_kwh=0.0,
                actual_grid_export_kwh=0.6,
                actual_charge_kwh=0.0,
                actual_discharge_kwh=0.0,
                actual_mode="idle",
                actual_reason="live_idle",
                soc_start_pct=50.0,
                soc_end_pct=50.0,
                samples=1,
            ),
            mod.Slot(
                ts=1_800_900,
                price_ct=40.0,
                residual_with_ev_kwh=0.6,
                dischargeable_load_kwh=0.6,
                pv_surplus_kwh=0.0,
                actual_grid_import_kwh=0.6,
                actual_grid_export_kwh=0.0,
                actual_charge_kwh=0.0,
                actual_discharge_kwh=0.0,
                actual_mode="idle",
                actual_reason="live_idle",
                soc_start_pct=50.0,
                soc_end_pct=50.0,
                samples=1,
            ),
        ]
        result = mod.optimize_perfect_foresight(
            slots,
            start_soc_pct=50.0,
            target_end_soc_pct=50.0,
            min_soc_pct=5.0,
            max_soc_pct=100.0,
            max_power_w=2400.0,
            eta_rt=0.80,
            feed_in_ct=0.0,
            allow_grid_charge=True,
        )
        self.assertGreater(result.slots[0].optimal_charge_kwh, 0.0)
        self.assertGreater(result.slots[1].optimal_discharge_kwh, 0.0)
        self.assertLess(result.optimal_cost_eur, result.actual_cost_eur)

    def test_terminal_soc_target_blocks_end_of_window_dumping(self):
        slots = [
            mod.Slot(
                ts=1_800_000,
                price_ct=80.0,
                residual_with_ev_kwh=0.6,
                dischargeable_load_kwh=0.6,
                pv_surplus_kwh=0.0,
                actual_grid_import_kwh=0.6,
                actual_grid_export_kwh=0.0,
                actual_charge_kwh=0.0,
                actual_discharge_kwh=0.0,
                actual_mode="idle",
                actual_reason="live_idle",
                soc_start_pct=50.0,
                soc_end_pct=50.0,
                samples=1,
            )
        ]
        result = mod.optimize_perfect_foresight(
            slots,
            start_soc_pct=50.0,
            target_end_soc_pct=50.0,
            min_soc_pct=5.0,
            max_soc_pct=100.0,
            max_power_w=2400.0,
            eta_rt=0.80,
            feed_in_ct=0.0,
            allow_grid_charge=True,
        )
        self.assertEqual(result.slots[0].optimal_discharge_kwh, 0.0)
        self.assertAlmostEqual(result.slots[0].soc_end_pct, 50.0, places=1)


if __name__ == "__main__":
    unittest.main()

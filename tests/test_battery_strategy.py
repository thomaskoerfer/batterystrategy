import importlib.util
import datetime as dt
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = (HERE.parent / 'battery_strategy_dryrun.py') if (HERE.parent / 'battery_strategy_dryrun.py').exists() else (HERE.parent / 'scripts' / 'battery_strategy_dryrun.py')
spec = importlib.util.spec_from_file_location('battery_strategy_dryrun', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class PlannedDispatchTests(unittest.TestCase):
    def test_idle_slot_does_not_inherit_discharge_mode(self):
        mode, power = mod.derive_planned_dispatch(
            {"mode": "idle", "power_w": 0.0},
            {"mode": "discharge_push"},
        )
        self.assertEqual(mode, "idle")
        self.assertEqual(power, 0)

    def test_idle_slot_becomes_discharge_blocked_when_budget_reserved(self):
        mode, power = mod.derive_planned_dispatch(
            {"mode": "idle", "power_w": 0.0},
            {"mode": "discharge_blocked", "remaining_discharge_budget_kwh": 1.2},
        )
        self.assertEqual(mode, "discharge_blocked")
        self.assertEqual(power, 0)

    def test_discharge_slot_uses_discharge_context_mode(self):
        mode, power = mod.derive_planned_dispatch(
            {"mode": "discharge", "power_w": -353.3},
            {"mode": "discharge_limited"},
        )
        self.assertEqual(mode, "discharge_limited")
        self.assertEqual(power, 353)

    def test_charge_slot_maps_to_charge_grid(self):
        mode, power = mod.derive_planned_dispatch(
            {"mode": "charge", "power_w": 1800.0},
            {"mode": "discharge_push"},
        )
        self.assertEqual(mode, "charge_grid")
        self.assertEqual(power, 1800)

    def test_mode_to_plan_seed(self):
        self.assertEqual(mod.mode_to_plan_seed("idle"), 0)
        self.assertEqual(mod.mode_to_plan_seed("charge_grid"), 1)
        self.assertEqual(mod.mode_to_plan_seed("charge_pv_surplus"), 1)
        self.assertEqual(mod.mode_to_plan_seed("discharge_push"), -1)
        self.assertEqual(mod.mode_to_plan_seed("discharge_limited"), -1)

    def test_grid_charge_requires_future_value_above_rte_and_margin(self):
        tz = dt.timezone(dt.timedelta(hours=1))
        start = dt.datetime(2026, 3, 14, 8, 0, tzinfo=tz)
        intervals = []
        for i, price_ct in enumerate([30.0, 30.4, 30.7, 30.6]):
            intervals.append(
                {
                    "dt": start + dt.timedelta(minutes=15 * i),
                    "price_eur": price_ct / 100.0,
                }
            )
        samples = []
        # Same-weekday history makes the current slots look cheap enough for the cheap-charge credit,
        # but there is still no future spread that beats losses plus margin.
        hist_prices = [36.0, 35.8, 36.2, 35.9]
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i, price_ct in enumerate(hist_prices):
                ts = (base + dt.timedelta(minutes=15 * i)).timestamp()
                samples.append(
                    {
                        "ts": ts,
                        "load_w": 500.0,
                        "house_w": 500.0,
                        "house_total_w": 500.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 500.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0,
                        "hp_w": 0.0,
                        "price_ct": price_ct,
                    }
                )
        plan = mod.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=mod.MIN_E_KWH,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * mod.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * mod.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )
        self.assertFalse(any(p["mode"] == "charge" for p in plan["points"]))

    def test_small_remaining_energy_prefers_current_near_peak_slot(self):
        future_points = [
            {"price_ct": 34.8, "grid_import_fc_w": 900.0, "discharge_fc_w": 0.0, "mode": "idle"},
            {"price_ct": 35.0, "grid_import_fc_w": 900.0, "discharge_fc_w": 0.0, "mode": "idle"},
            {"price_ct": 35.1, "grid_import_fc_w": 900.0, "discharge_fc_w": 0.0, "mode": "idle"},
        ]
        ctx = mod.classify_discharge_mode(future_points, 34.8, 0.14)
        self.assertNotEqual(ctx["mode"], "discharge_blocked")
        self.assertGreater(ctx["current_allocated_power_w"], 0.0)

    def test_local_dt_from_ts_uses_berlin_timezone(self):
        dt_obj = mod.local_dt_from_ts(1773529200)  # 2026-03-14 23:00:00 UTC
        self.assertEqual(dt_obj.tzinfo.key, "Europe/Berlin")
        self.assertEqual((dt_obj.year, dt_obj.month, dt_obj.day, dt_obj.hour, dt_obj.minute), (2026, 3, 15, 0, 0))


if __name__ == '__main__':
    unittest.main()

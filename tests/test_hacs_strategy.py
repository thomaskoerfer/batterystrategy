import unittest
import datetime as dt
import tempfile
import json
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.const import (
    COMMAND_IDLE,
    COMMAND_INPUT,
    COMMAND_OUTPUT,
    DISCHARGE_LOAD,
    DISCHARGE_OFF,
    DISCHARGE_PRICE_SENSITIVE,
    CONF_ZENDURE_INPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
    GRID_CHARGING_OFF,
    GRID_CHARGING_PRICE_SENSITIVE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    PV_CHARGING_OFF,
    PV_CHARGING_ON,
)
from custom_components.battery_strategy.forecast import (
    clamp_bias,
    fallback_weather_factor,
)
from custom_components.battery_strategy.models import (
    StrategyCommand,
    StrategyInputs,
    StrategyOptions,
)
from custom_components.battery_strategy import optimizer_adapter
from custom_components.battery_strategy import optimizer_engine
from custom_components.battery_strategy.optimizer_state import (
    load_state_document,
    save_state_document,
)
from custom_components.battery_strategy.planner import BackgroundPlanner
from custom_components.battery_strategy.plan_models import (
    PlanLiveDirective,
    PlanPoint,
    StrategyPlan,
)
from custom_components.battery_strategy.pricing import read_tibber_price_points
from custom_components.battery_strategy import sensor as battery_sensor
from custom_components.battery_strategy.actuator import (
    should_write_limit,
    should_write_mode,
    zendure_targets,
)
from custom_components.battery_strategy.coordinator import (
    BatteryStrategyCoordinator,
    _load_last_known_soc_pct,
)
from custom_components.battery_strategy import _migrate_runtime_files
from custom_components.battery_strategy.strategy import (
    calculate_command,
    live_command_from_plan,
    plan_live_directive_from_plan,
)


class HacsStrategyTests(unittest.TestCase):
    def test_slot_progress_accounts_measured_battery_power(self):
        coordinator = object.__new__(BatteryStrategyCoordinator)
        now = dt.datetime.now(dt.timezone.utc)
        coordinator._last_live_accounting_ts = now - dt.timedelta(seconds=36)
        coordinator._last_actual_battery_power_w = -1000.0
        coordinator._slot_charged_kwh = 0.0
        coordinator._slot_discharged_kwh = 0.0
        coordinator._account_actual_battery_power(now)
        self.assertAlmostEqual(coordinator._slot_charged_kwh, 0.01, places=4)
        self.assertEqual(coordinator._slot_discharged_kwh, 0.0)

    def test_failsafe_zeros_limits_only_once_per_fault(self):
        calls = []

        class Services:
            @staticmethod
            async def async_call(domain, service, data, blocking=False):
                calls.append((domain, service, data, blocking))

        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator.hass = SimpleNamespace(services=Services())
        coordinator._failsafe_zeroed_reason = None
        coordinator._entity_id = lambda key: key
        coordinator._state_available = lambda _entity: True
        coordinator._raw_state_float = lambda _entity: 200.0

        async def scenario():
            await coordinator._async_failsafe_zero_once("grid_inputs_stale")
            await coordinator._async_failsafe_zero_once("grid_inputs_stale")

        asyncio.run(scenario())
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[2]["value"] == 0 for call in calls))
        self.assertEqual(coordinator.last_actuation["status"], "failsafe_no_write")

    def test_optimizer_state_migrates_plain_json_to_atomic_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "optimizer-state.json"
            original = {
                "samples": [{"soc": 42.0}] * 100,
                "last_output": {"mode": "idle"},
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(load_state_document(path), original)
            save_state_document(path, original)
            self.assertEqual(path.read_bytes()[:2], b"\x1f\x8b")
            self.assertEqual(load_state_document(path), original)

    def test_full_optimizer_honors_disabled_action_policies(self):
        original = (
            optimizer_engine.PV_CHARGING_ENABLED,
            optimizer_engine.GRID_CHARGING_ENABLED,
            optimizer_engine.DISCHARGE_ENABLED,
        )
        optimizer_engine.PV_CHARGING_ENABLED = False
        optimizer_engine.GRID_CHARGING_ENABLED = False
        optimizer_engine.DISCHARGE_ENABLED = False
        try:
            start = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
            intervals = [
                {
                    "dt": start + dt.timedelta(minutes=15 * i),
                    "price_eur": (5.0 if i < 4 else 50.0) / 100.0,
                }
                for i in range(8)
            ]
            samples = [
                {
                    "ts": (
                        start - dt.timedelta(days=7) + dt.timedelta(minutes=15 * i)
                    ).timestamp(),
                    "load_w": 800.0,
                    "pv_w": 0.0,
                    "price_ct": 30.0,
                }
                for i in range(8)
            ]
            plan = optimizer_engine.build_virtual_plan(
                intervals,
                samples,
                3.0,
                1.0,
                None,
                1.0,
                [1.0] * optimizer_engine.SLOTS_PER_DAY,
                [1.0] * optimizer_engine.SLOTS_PER_DAY,
                now_local=start,
            )
            self.assertTrue(all(point["charge_fc_w"] == 0 for point in plan["points"]))
            self.assertTrue(
                all(point["discharge_fc_w"] == 0 for point in plan["points"])
            )
        finally:
            (
                optimizer_engine.PV_CHARGING_ENABLED,
                optimizer_engine.GRID_CHARGING_ENABLED,
                optimizer_engine.DISCHARGE_ENABLED,
            ) = original

    def test_background_planner_serves_cached_plan_without_waiting(self):
        class Adapter:
            def __init__(self):
                self.finished = False

            def hydrate(self, _path):
                return None

            def needs_run(self, _options, force=False):
                return not self.finished or force

            def run(self, *_args):
                time.sleep(0.1)
                self.finished = True

            def cached_result(self, _inputs, _options):
                return StrategyPlan([], COMMAND_IDLE, 0, "cached"), {}

        class Hass:
            @staticmethod
            def async_add_executor_job(target, *args):
                return asyncio.get_running_loop().run_in_executor(None, target, *args)

        async def scenario():
            planner = BackgroundPlanner(Hass(), Adapter())
            inputs = StrategyInputs(0, 0, 0, 0)
            options = StrategyOptions()
            started = time.monotonic()
            self.assertTrue(planner.maybe_schedule(inputs, options, {}))
            plan, _ = planner.current(inputs, options)
            self.assertEqual(plan.reason, "cached")
            self.assertLess(time.monotonic() - started, 0.05)
            await asyncio.sleep(0.15)
            self.assertFalse(planner.running)
            await planner.async_shutdown()

        asyncio.run(scenario())

    def test_runtime_file_migration_compacts_legacy_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "battery_strategy_hacs_command_trace.json"
            legacy.write_text(
                json.dumps([{"ts": 1, "mode": "idle"}, {"ts": 2, "mode": "input"}])
            )
            _migrate_runtime_files(tmp)
            current = root / "battery_strategy_command_trace.jsonl"
            lines = [json.loads(line) for line in current.read_text().splitlines()]
            self.assertEqual(
                lines, [{"ts": 1, "mode": "idle"}, {"ts": 2, "mode": "input"}]
            )
            self.assertFalse(legacy.exists())

    def test_profile_attrs_prefer_raw_unfiltered_optimizer_profiles(self):
        today = dt.datetime.now().date().isoformat()
        raw_price = [[1_800_000_000_000, 31.2], [1_800_000_900_000, 32.4]]
        data = {
            "optimizer_attrs": {
                "profile_today_price": raw_price,
                "profile_today_soc": [[1_800_000_000_000, 40.0]],
                "profile_today_pv_actual_power": [[1_800_000_000_000, 120.0]],
                "profile_today_house_actual_power": [[1_800_000_000_000, 230.0]],
            },
            "plan": StrategyPlan(
                points=[
                    PlanPoint(
                        ts_ms=1_800_000_900_000,
                        date=today,
                        price_ct=32.4,
                        load_fc_w=0,
                        pv_fc_w=0,
                        grid_import_fc_w=0,
                        grid_export_fc_w=0,
                        grid_net_fc_w=0,
                        mode=COMMAND_IDLE,
                        power_w=0,
                        charge_fc_w=0,
                        discharge_fc_w=0,
                        soc_pct=41.0,
                    )
                ],
                current_mode=COMMAND_IDLE,
                current_power_w=0,
                reason="test",
            ),
        }

        attrs = battery_sensor._profile_attrs(data, today)

        self.assertEqual(attrs["price"], raw_price)
        self.assertEqual(attrs["soc"], [[1_800_000_000_000, 40.0]])
        self.assertEqual(attrs["pv_actual_power"], [[1_800_000_000_000, 120.0]])
        self.assertEqual(attrs["house_actual_power"], [[1_800_000_000_000, 230.0]])

    def test_optimizer_discharge_budget_sensor_is_separate_from_live_remaining_budget(
        self,
    ):
        today = dt.datetime.now().date().isoformat()
        data = {
            "plan": StrategyPlan(
                points=[
                    PlanPoint(
                        ts_ms=1_800_000_000_000,
                        date=today,
                        price_ct=35.0,
                        load_fc_w=1000,
                        pv_fc_w=0,
                        grid_import_fc_w=1000,
                        grid_export_fc_w=0,
                        grid_net_fc_w=1000,
                        mode=COMMAND_IDLE,
                        power_w=0,
                        charge_fc_w=0,
                        discharge_fc_w=0,
                        soc_pct=60.0,
                        discharge_budget_kwh=0.6,
                    )
                ],
                current_mode=COMMAND_IDLE,
                current_power_w=0,
                reason="test",
            ),
            "plan_to_live": PlanLiveDirective(
                slot_id="slot",
                slot_start_ts=1_800_000_000_000,
                slot_end_ts=1_800_000_900_000,
                pv_charge_allowed=True,
                must_charge_w=0,
                must_charge_remaining_kwh=0.0,
                grid_charge_allowed=False,
                discharge_budget_kwh=0.2,
                battery_min_soc_pct=10.0,
                battery_max_soc_pct=100.0,
            ),
        }

        self.assertEqual(battery_sensor._optimizer_discharge_budget_kwh(data), 0.6)
        self.assertEqual(battery_sensor._plan_to_live(data).discharge_budget_kwh, 0.2)

    def _coordinator_for_strategy_enabled(self, strategy_enabled=True):
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator.entry = SimpleNamespace(
            options={"strategy_enabled": strategy_enabled}
        )
        return coordinator

    def test_strategy_disabled_live_display_is_idle_with_external_source(self):
        coordinator = self._coordinator_for_strategy_enabled(False)
        calculated = StrategyCommand(
            mode=COMMAND_OUTPUT,
            power_w=850,
            reason="budget_discharge",
            residual_with_ev_w=850,
            residual_no_ev_w=850,
            pv_surplus_w=0,
            allowed_discharge_load_w=850,
            house_load_total_w=850,
            house_load_no_ev_w=850,
        )

        display = coordinator._disabled_display_command(calculated)
        data = {"strategy_enabled": False, "send_commands": False, "command": display}

        self.assertEqual(display.mode, COMMAND_IDLE)
        self.assertEqual(display.power_w, 0)
        self.assertEqual(display.reason, "strategy_disabled_external_control")
        self.assertEqual(
            battery_sensor._command_source(data), "external_control_strategy_disabled"
        )

    def test_strategy_disabled_zeroes_once_then_stays_hands_off(self):
        async def run_case():
            calls = []

            class FakeState:
                def __init__(self, state):
                    self.state = state
                    self.last_changed = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                        seconds=60
                    )

            class FakeStates:
                def __init__(self):
                    self.values = {
                        "number.battery_input_limit": FakeState("400"),
                        "number.battery_output_limit": FakeState("300"),
                    }

                def get(self, entity_id):
                    return self.values.get(entity_id)

            class FakeServices:
                async def async_call(self, domain, service, data, blocking=False):
                    calls.append((domain, service, data, blocking))

            coordinator = object.__new__(BatteryStrategyCoordinator)
            coordinator.hass = SimpleNamespace(
                states=FakeStates(), services=FakeServices()
            )
            coordinator.entry = SimpleNamespace(
                data={
                    CONF_ZENDURE_INPUT_LIMIT_ENTITY: "number.battery_input_limit",
                    CONF_ZENDURE_OUTPUT_LIMIT_ENTITY: "number.battery_output_limit",
                },
                options={"strategy_enabled": False},
            )
            coordinator.last_actuation = {"status": "not_started"}
            coordinator._strategy_was_enabled = True
            coordinator._disabled_zeroed = False

            await coordinator._async_zero_limits_once()
            coordinator._strategy_was_enabled = False
            coordinator._disabled_zeroed = True

            self.assertEqual(len(calls), 2)
            self.assertEqual(
                calls[0][2], {"entity_id": "number.battery_input_limit", "value": 0}
            )
            self.assertEqual(
                calls[1][2], {"entity_id": "number.battery_output_limit", "value": 0}
            )
            self.assertEqual(coordinator.last_actuation["status"], "disabled_zeroed")

            calls.clear()
            if coordinator._strategy_was_enabled and not coordinator._disabled_zeroed:
                await coordinator._async_zero_limits_once()

            self.assertEqual(calls, [])

        asyncio.run(run_case())

    def test_current_simple_mode_discharges_against_load_without_feeding_ev(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=5000,
                grid_export_w=0,
                pv_w=0,
                battery_power_w=0,
                ev_power_w=4200,
                soc_pct=80,
            ),
            StrategyOptions(
                pv_charging=PV_CHARGING_ON,
                discharge=DISCHARGE_LOAD,
                battery_may_feed_ev=False,
                ev_active_threshold_w=300,
            ),
        )
        self.assertEqual(cmd.mode, COMMAND_OUTPUT)
        self.assertEqual(cmd.power_w, 800)
        self.assertEqual(cmd.allowed_discharge_load_w, 800)
        self.assertEqual(cmd.residual_with_ev_w, 5000)
        self.assertEqual(cmd.residual_no_ev_w, 800)

    def test_current_simple_mode_does_not_discharge_when_only_ev_load_exists(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=4200,
                grid_export_w=0,
                pv_w=0,
                battery_power_w=0,
                ev_power_w=4200,
                soc_pct=80,
            ),
            StrategyOptions(discharge=DISCHARGE_LOAD, battery_may_feed_ev=False),
        )
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.power_w, 0)

    def test_ev_policy_can_block_all_automatic_discharge_while_ev_is_active(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=5000,
                grid_export_w=0,
                pv_w=0,
                battery_power_w=0,
                ev_power_w=4200,
                soc_pct=80,
            ),
            StrategyOptions(
                discharge=DISCHARGE_LOAD,
                discharge_during_ev_charging=False,
                battery_may_feed_ev=True,
                ev_active_threshold_w=300,
            ),
        )
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.power_w, 0)
        self.assertEqual(cmd.allowed_discharge_load_w, 0)

    def test_ev_discharge_block_is_visible_for_an_open_live_budget(self):
        inputs = StrategyInputs(
            grid_import_w=5000,
            grid_export_w=0,
            pv_w=0,
            battery_power_w=0,
            ev_power_w=4200,
            soc_pct=80,
        )
        options = StrategyOptions(
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=False,
            battery_may_feed_ev=False,
            ev_active_threshold_w=300,
        )
        diagnostics = calculate_command(inputs, options)
        cmd = live_command_from_plan(
            StrategyPlan(
                points=[], current_mode=COMMAND_IDLE, current_power_w=0, reason="test"
            ),
            diagnostics,
            inputs,
            options,
        )
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.power_w, 0)
        self.assertEqual(cmd.reason, "ev_discharge_blocked")

    def test_ev_discharge_block_does_not_block_live_pv_surplus_charging(self):
        inputs = StrategyInputs(
            grid_import_w=0,
            grid_export_w=500,
            pv_w=1500,
            battery_power_w=0,
            ev_power_w=1000,
            soc_pct=50,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            discharge=DISCHARGE_LOAD,
            discharge_during_ev_charging=False,
            battery_may_feed_ev=False,
            ev_active_threshold_w=300,
        )
        diagnostics = calculate_command(inputs, options)
        cmd = live_command_from_plan(
            StrategyPlan(
                points=[], current_mode=COMMAND_IDLE, current_power_w=0, reason="test"
            ),
            diagnostics,
            inputs,
            options,
        )
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 500)
        self.assertEqual(cmd.reason, "live_pv_surplus")

    def test_live_command_clamps_stale_plan_discharge_to_current_load(self):
        inputs = StrategyInputs(
            grid_import_w=180,
            grid_export_w=0,
            pv_w=4,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=70,
        )
        options = StrategyOptions(discharge=DISCHARGE_LOAD, max_discharge_power_w=2400)
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan(
            points=[],
            current_mode=COMMAND_OUTPUT,
            current_power_w=2046,
            reason="stale optimizer slot",
        )
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_OUTPUT)
        self.assertEqual(cmd.power_w, 180)
        self.assertEqual(cmd.reason, "budget_discharge")

    def test_live_command_load_mode_overrides_plan_reservation(self):
        inputs = StrategyInputs(
            grid_import_w=8186,
            grid_export_w=0,
            pv_w=600,
            battery_power_w=0,
            ev_power_w=4700,
            soc_pct=99,
        )
        options = StrategyOptions(
            discharge=DISCHARGE_LOAD,
            battery_may_feed_ev=False,
            ev_active_threshold_w=300,
            max_discharge_power_w=2400,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan(
            points=[],
            current_mode=COMMAND_IDLE,
            current_power_w=0,
            reason="battery reserved for later higher-value slots",
        )
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_OUTPUT)
        self.assertEqual(cmd.power_w, 2400)

    def test_live_command_pv_charge_does_not_follow_plan_into_grid_import(self):
        inputs = StrategyInputs(
            grid_import_w=656,
            grid_export_w=0,
            pv_w=1441,
            battery_power_w=-789,
            ev_power_w=0,
            soc_pct=62,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            grid_charging=GRID_CHARGING_OFF,
            discharge=DISCHARGE_OFF,
            max_charge_power_w=2400,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan(
            points=[],
            current_mode=COMMAND_INPUT,
            current_power_w=1672,
            reason="stale optimizer slot",
        )
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 133)

    def test_must_charge_blocks_discharge_and_charges_from_grid(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            18.2,
            1000,
            0,
            2453,
            0,
            2453,
            COMMAND_INPUT,
            1453,
            1453,
            0,
            33.0,
        )
        inputs = StrategyInputs(
            grid_import_w=2809,
            grid_export_w=0,
            pv_w=1439,
            battery_power_w=2398,
            ev_power_w=0,
            soc_pct=33,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
            discharge=DISCHARGE_PRICE_SENSITIVE,
            min_soc_pct=5,
            max_discharge_power_w=2400,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan(
            points=[point],
            current_mode=COMMAND_INPUT,
            current_power_w=1453,
            reason="15min Tibber plan",
        )
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 1453)
        self.assertEqual(cmd.reason, "must_charge")

    def test_must_charge_defers_when_same_price_slots_have_capacity(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int((now + dt.timedelta(minutes=15 * i)).timestamp() * 1000),
                now.date().isoformat(),
                18.2,
                1000,
                0,
                2000 if i == 0 else 1000,
                0,
                2000 if i == 0 else 1000,
                COMMAND_INPUT if i == 0 else COMMAND_IDLE,
                1000 if i == 0 else 0,
                1000 if i == 0 else 0,
                0,
                33.0,
            )
            for i in range(4)
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(points, COMMAND_INPUT, 1000, "deferable cheap grid slot"),
            StrategyOptions(
                pv_charging=PV_CHARGING_ON,
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                max_charge_power_w=2400,
            ),
        )
        self.assertFalse(directive.grid_charge_allowed)
        self.assertEqual(directive.must_charge_w, 0)
        self.assertEqual(directive.must_charge_remaining_kwh, 0.0)

    def test_plan_live_directive_describes_only_required_live_inputs(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            18.2,
            500,
            1000,
            1200,
            0,
            1200,
            COMMAND_INPUT,
            1700,
            1700,
            0,
            60.0,
        )
        plan = StrategyPlan(
            points=[point],
            current_mode=COMMAND_INPUT,
            current_power_w=1700,
            reason="15min Tibber plan",
        )
        directive = plan_live_directive_from_plan(
            plan,
            StrategyOptions(
                pv_charging=PV_CHARGING_ON,
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=5,
                max_charge_power_w=2400,
                max_discharge_power_w=2400,
            ),
        )
        self.assertEqual(directive.slot_start_ts, point.ts_ms)
        self.assertEqual(directive.slot_end_ts, point.ts_ms + 15 * 60 * 1000)
        self.assertTrue(directive.pv_charge_allowed)
        self.assertTrue(directive.grid_charge_allowed)
        self.assertEqual(directive.must_charge_w, 1700)
        self.assertEqual(directive.must_charge_remaining_kwh, 0.425)
        self.assertEqual(directive.discharge_budget_kwh, 0.0)
        self.assertEqual(directive.battery_min_soc_pct, 5)
        self.assertEqual(directive.battery_max_soc_pct, 100)

    def test_price_sensitive_discharge_uses_plan_budget_not_soc_floor(self):
        now = dt.datetime(2026, 5, 29, 18, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            31.57,
            2500,
            800,
            1700,
            0,
            1700,
            COMMAND_IDLE,
            0,
            0,
            0,
            98.0,
        )
        inputs = StrategyInputs(
            grid_import_w=2600,
            grid_export_w=0,
            pv_w=800,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=97.5,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
            discharge=DISCHARGE_PRICE_SENSITIVE,
            min_soc_pct=5,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan(
            [point], COMMAND_IDLE, 0, "reserved for later higher-value slots"
        )
        directive = plan_live_directive_from_plan(plan, options)
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(directive.discharge_budget_kwh, 0.0)
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.reason, "live_idle")

    def test_price_sensitive_discharge_budget_comes_from_plan_not_spill_heuristic(self):
        now = dt.datetime(2026, 5, 29, 14, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int(now.timestamp() * 1000),
                now.date().isoformat(),
                20.0,
                500,
                2000,
                0,
                1500,
                -1500,
                COMMAND_INPUT,
                0,
                0,
                0,
                85.0,
            )
        ]
        for i in range(1, 5):
            slot = now + dt.timedelta(minutes=15 * i)
            points.append(
                PlanPoint(
                    int(slot.timestamp() * 1000),
                    slot.date().isoformat(),
                    21.0,
                    500,
                    2000,
                    0,
                    1500,
                    -1500,
                    COMMAND_INPUT,
                    0,
                    0,
                    0,
                    90.0,
                )
            )
        inputs = StrategyInputs(
            grid_import_w=3000,
            grid_export_w=0,
            pv_w=0,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=85,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
            discharge=DISCHARGE_PRICE_SENSITIVE,
            min_soc_pct=5,
            max_discharge_power_w=2400,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan(points, COMMAND_IDLE, 0, "pv surplus later")
        directive = plan_live_directive_from_plan(plan, options)
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(directive.discharge_budget_kwh, 0.0)
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.reason, "live_idle")

    def test_price_sensitive_discharge_fc_does_not_create_live_budget(self):
        now = dt.datetime(2026, 7, 12, 20, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            35.0,
            1200,
            0,
            1200,
            0,
            1200,
            COMMAND_OUTPUT,
            1200,
            0,
            1200,
            80.0,
            discharge_budget_kwh=0.0,
        )
        plan = StrategyPlan(
            [point], COMMAND_OUTPUT, 1200, "planned discharge but no budget"
        )
        options = StrategyOptions(discharge=DISCHARGE_PRICE_SENSITIVE, min_soc_pct=10)
        inputs = StrategyInputs(
            grid_import_w=1200,
            grid_export_w=0,
            pv_w=0,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=80,
        )
        diagnostics = calculate_command(inputs, options)

        directive = plan_live_directive_from_plan(plan, options)
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)

        self.assertEqual(directive.discharge_budget_kwh, 0.0)
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.power_w, 0)

    def test_current_slot_discharge_budget_cannot_increase_on_reoptimization(self):
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator._active_directive_slot_id = None
        coordinator._active_directive_slot_end_ts_ms = 0
        coordinator._slot_charged_kwh = 0.0
        coordinator._slot_discharged_kwh = 0.0
        coordinator._active_discharge_budget_base_kwh = 0.0

        def directive(slot_id, budget):
            return PlanLiveDirective(
                slot_id=slot_id,
                slot_start_ts=1,
                slot_end_ts=2,
                pv_charge_allowed=True,
                must_charge_w=0,
                must_charge_remaining_kwh=0.0,
                grid_charge_allowed=False,
                discharge_budget_kwh=budget,
                battery_min_soc_pct=10.0,
                battery_max_soc_pct=100.0,
            )

        first = coordinator._directive_with_progress(directive("slot-a", 0.10))
        coordinator._slot_discharged_kwh = 0.04
        raised = coordinator._directive_with_progress(directive("slot-a", 0.20))
        lowered = coordinator._directive_with_progress(directive("slot-a", 0.05))
        next_slot = coordinator._directive_with_progress(directive("slot-b", 0.20))

        self.assertEqual(first.discharge_budget_kwh, 0.10)
        self.assertEqual(raised.discharge_budget_kwh, 0.06)
        self.assertEqual(lowered.discharge_budget_kwh, 0.01)
        self.assertEqual(next_slot.discharge_budget_kwh, 0.20)

    def test_load_discharge_can_open_budget_inside_current_slot(self):
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator._active_directive_slot_id = None
        coordinator._active_directive_slot_end_ts_ms = 0
        coordinator._slot_charged_kwh = 0.0
        coordinator._slot_discharged_kwh = 0.0
        coordinator._active_discharge_budget_base_kwh = 0.0

        def directive(slot_id, budget):
            return PlanLiveDirective(
                slot_id=slot_id,
                slot_start_ts=1,
                slot_end_ts=2,
                pv_charge_allowed=True,
                must_charge_w=0,
                must_charge_remaining_kwh=0.0,
                grid_charge_allowed=False,
                discharge_budget_kwh=budget,
                battery_min_soc_pct=10.0,
                battery_max_soc_pct=100.0,
            )

        first = coordinator._directive_with_progress(directive("slot-a", 0.0))
        opened = coordinator._directive_with_progress(
            directive("slot-a", 0.6),
            allow_discharge_budget_increase=True,
        )

        self.assertEqual(first.discharge_budget_kwh, 0.0)
        self.assertEqual(opened.discharge_budget_kwh, 0.6)

    def test_discharge_mode_change_resets_current_slot_budget_context(self):
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator._active_directive_slot_id = None
        coordinator._active_directive_slot_end_ts_ms = 0
        coordinator._slot_charged_kwh = 0.0
        coordinator._slot_discharged_kwh = 0.0
        coordinator._active_discharge_budget_base_kwh = 0.0
        coordinator._active_discharge_mode = None

        def directive(slot_id, budget):
            return PlanLiveDirective(
                slot_id=slot_id,
                slot_start_ts=1,
                slot_end_ts=2,
                pv_charge_allowed=True,
                must_charge_w=0,
                must_charge_remaining_kwh=0.0,
                grid_charge_allowed=False,
                discharge_budget_kwh=budget,
                battery_min_soc_pct=10.0,
                battery_max_soc_pct=100.0,
            )

        load_open = coordinator._directive_with_progress(
            directive("slot-a", 0.6),
            discharge_mode=DISCHARGE_LOAD,
            allow_discharge_budget_increase=True,
        )
        price_sensitive = coordinator._directive_with_progress(
            directive("slot-a", 0.05),
            discharge_mode=DISCHARGE_PRICE_SENSITIVE,
        )

        self.assertEqual(load_open.discharge_budget_kwh, 0.6)
        self.assertEqual(price_sensitive.discharge_budget_kwh, 0.05)

    def test_price_sensitive_low_price_does_not_use_pv_charge_as_free_replacement(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int(now.timestamp() * 1000),
                now.date().isoformat(),
                24.0,
                700,
                1200,
                0,
                0,
                0,
                COMMAND_INPUT,
                400,
                400,
                0,
                50.0,
            ),
            PlanPoint(
                int((now + dt.timedelta(minutes=15)).timestamp() * 1000),
                now.date().isoformat(),
                24.0,
                700,
                2200,
                0,
                0,
                0,
                COMMAND_INPUT,
                1500,
                1500,
                0,
                55.0,
            ),
            PlanPoint(
                int((now + dt.timedelta(minutes=30)).timestamp() * 1000),
                now.date().isoformat(),
                36.0,
                1500,
                0,
                1000,
                0,
                1000,
                COMMAND_OUTPUT,
                500,
                0,
                500,
                60.0,
            ),
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                points,
                COMMAND_INPUT,
                900,
                "pv charge is already allocated",
                price_stats={"p_high": 34.0, "discharge_floor_ct": 30.0},
            ),
            StrategyOptions(
                pv_charging=PV_CHARGING_ON,
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=5,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.0)

    def test_price_sensitive_low_price_does_not_open_without_plan_budget(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int(now.timestamp() * 1000),
                now.date().isoformat(),
                24.0,
                700,
                1200,
                0,
                0,
                0,
                COMMAND_INPUT,
                400,
                400,
                0,
                50.0,
            ),
        ]
        for i in range(1, 5):
            points.append(
                PlanPoint(
                    int((now + dt.timedelta(minutes=15 * i)).timestamp() * 1000),
                    now.date().isoformat(),
                    23.0,
                    500,
                    2000,
                    0,
                    1500,
                    -1500,
                    COMMAND_IDLE,
                    0,
                    0,
                    0,
                    55.0,
                )
            )
        points.append(
            PlanPoint(
                int((now + dt.timedelta(minutes=75)).timestamp() * 1000),
                now.date().isoformat(),
                36.0,
                1500,
                0,
                1000,
                0,
                1000,
                COMMAND_OUTPUT,
                500,
                0,
                500,
                60.0,
            )
        )
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                points,
                COMMAND_INPUT,
                900,
                "spill before expensive slot",
                price_stats={"p_high": 34.0, "discharge_floor_ct": 30.0},
            ),
            StrategyOptions(
                pv_charging=PV_CHARGING_ON,
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=5,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.0)

    def test_price_sensitive_low_price_rejects_grid_replacement_after_losses(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int(now.timestamp() * 1000),
                now.date().isoformat(),
                24.0,
                700,
                0,
                700,
                0,
                700,
                COMMAND_IDLE,
                0,
                0,
                0,
                50.0,
            ),
            PlanPoint(
                int((now + dt.timedelta(minutes=15)).timestamp() * 1000),
                now.date().isoformat(),
                22.0,
                500,
                0,
                500,
                0,
                500,
                COMMAND_INPUT,
                1200,
                1200,
                0,
                50.0,
            ),
            PlanPoint(
                int((now + dt.timedelta(minutes=30)).timestamp() * 1000),
                now.date().isoformat(),
                34.0,
                1500,
                0,
                1000,
                0,
                1000,
                COMMAND_OUTPUT,
                500,
                0,
                500,
                60.0,
            ),
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                points,
                COMMAND_IDLE,
                0,
                "grid replacement is not cheap after losses",
                price_stats={"p_high": 32.0, "discharge_floor_ct": 30.0},
            ),
            StrategyOptions(
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                round_trip_efficiency=0.8,
                min_margin_ct_per_kwh=2.0,
                min_soc_pct=5,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.0)

    def test_price_sensitive_planned_discharge_does_not_create_budget_floor(self):
        now = dt.datetime(2026, 5, 29, 9, 30, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int(now.timestamp() * 1000),
                now.date().isoformat(),
                20.64,
                886,
                402,
                37,
                0,
                37,
                COMMAND_OUTPUT,
                447,
                0,
                447,
                6.92,
            ),
            PlanPoint(
                int((now + dt.timedelta(minutes=15)).timestamp() * 1000),
                now.date().isoformat(),
                19.36,
                482,
                402,
                0,
                0,
                0,
                COMMAND_INPUT,
                270,
                270,
                0,
                6.92,
            ),
            PlanPoint(
                int((now + dt.timedelta(minutes=30)).timestamp() * 1000),
                now.date().isoformat(),
                19.88,
                358,
                398,
                0,
                39,
                -39,
                COMMAND_IDLE,
                0,
                0,
                0,
                7.93,
            ),
            PlanPoint(
                int((now + dt.timedelta(hours=11)).timestamp() * 1000),
                now.date().isoformat(),
                32.43,
                200,
                0,
                111,
                0,
                111,
                COMMAND_OUTPUT,
                89,
                0,
                89,
                50.0,
            ),
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                points,
                COMMAND_OUTPUT,
                447,
                "low price planned discharge must still be replaceable",
                price_stats={"p_high": 30.69, "discharge_floor_ct": 24.738},
            ),
            StrategyOptions(
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                round_trip_efficiency=0.8,
                min_margin_ct_per_kwh=2.0,
                min_soc_pct=5,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.0)

    def test_price_sensitive_grid_replacement_budget_must_be_in_plan(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int(now.timestamp() * 1000),
                now.date().isoformat(),
                32.0,
                700,
                0,
                700,
                0,
                700,
                COMMAND_IDLE,
                0,
                0,
                0,
                50.0,
            ),
            PlanPoint(
                int((now + dt.timedelta(minutes=15)).timestamp() * 1000),
                now.date().isoformat(),
                10.0,
                500,
                0,
                500,
                0,
                500,
                COMMAND_IDLE,
                0,
                0,
                0,
                50.0,
            ),
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                points,
                COMMAND_IDLE,
                0,
                "cheap grid replacement",
                price_stats={"p_high": 40.0, "discharge_floor_ct": 38.0},
            ),
            StrategyOptions(
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                round_trip_efficiency=0.8,
                min_margin_ct_per_kwh=2.0,
                max_charge_power_w=2400,
                min_soc_pct=5,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.0)

    def test_price_sensitive_pv_recharge_budget_keeps_export_reserve(self):
        now = dt.datetime(2026, 5, 29, 14, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int((now + dt.timedelta(minutes=15 * i)).timestamp() * 1000),
                now.date().isoformat(),
                20.0,
                500,
                800,
                0,
                250 if i > 0 else 0,
                -250 if i > 0 else 0,
                COMMAND_IDLE,
                0,
                0,
                0,
                85.0,
            )
            for i in range(4)
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(points, COMMAND_IDLE, 0, "small pv surplus later"),
            StrategyOptions(
                pv_charging=PV_CHARGING_ON,
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=5,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.0)

    def test_pv_recharge_budget_ignores_export_created_by_planned_discharge(self):
        now = dt.datetime(2026, 5, 29, 14, tzinfo=dt.timezone.utc)
        current = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            20.0,
            500,
            400,
            100,
            0,
            100,
            COMMAND_IDLE,
            0,
            0,
            0,
            85.0,
        )
        future = PlanPoint(
            int((now + dt.timedelta(minutes=15)).timestamp() * 1000),
            now.date().isoformat(),
            20.0,
            500,
            400,
            0,
            1200,
            -1200,
            COMMAND_IDLE,
            0,
            0,
            0,
            85.0,
        )
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                [current, future],
                COMMAND_IDLE,
                0,
                "export caused by discharge must not count",
            ),
            StrategyOptions(
                pv_charging=PV_CHARGING_ON,
                grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=5,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.0)

    def test_price_sensitive_current_discharge_follows_meter_with_explicit_budget(self):
        now = dt.datetime(2026, 5, 29, 21, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            41.0,
            700,
            0,
            450,
            0,
            450,
            COMMAND_OUTPUT,
            250,
            0,
            250,
            92.0,
            discharge_budget_kwh=0.112,
        )
        inputs = StrategyInputs(
            grid_import_w=450,
            grid_export_w=0,
            pv_w=0,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=95,
        )
        options = StrategyOptions(
            discharge=DISCHARGE_PRICE_SENSITIVE, max_discharge_power_w=2400
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan(
            [point],
            COMMAND_OUTPUT,
            250,
            "expensive slot",
            price_stats={"p_high": 35.0, "discharge_floor_ct": 30.0},
        )
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_OUTPUT)
        self.assertEqual(cmd.power_w, 450)
        self.assertEqual(cmd.reason, "budget_discharge")

    def test_price_sensitive_highest_price_block_releases_available_energy(self):
        now = dt.datetime(2026, 5, 29, 21, tzinfo=dt.timezone.utc)
        points = [
            PlanPoint(
                int((now + dt.timedelta(minutes=15 * i)).timestamp() * 1000),
                now.date().isoformat(),
                80.0 if i == 0 else 45.0,
                2400,
                0,
                2400,
                0,
                2400,
                COMMAND_OUTPUT if i == 0 else COMMAND_IDLE,
                2400 if i == 0 else 0,
                0,
                2400 if i == 0 else 0,
                90.0,
                4.8 if i == 0 else 0.0,
            )
            for i in range(5)
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                points,
                COMMAND_OUTPUT,
                2400,
                "highest value block",
                price_stats={"p_high": 40.0, "discharge_floor_ct": 36.0},
            ),
            StrategyOptions(
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=10,
                max_discharge_power_w=2400,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 4.8)

    def test_price_sensitive_budget_reserves_for_later_higher_value_slots(self):
        now = dt.datetime(2026, 5, 29, 21, tzinfo=dt.timezone.utc)
        prices = [45.0, 35.0, 80.0, 80.0, 80.0, 80.0]
        points = [
            PlanPoint(
                int((now + dt.timedelta(minutes=15 * i)).timestamp() * 1000),
                now.date().isoformat(),
                prices[i],
                2400,
                0,
                2400,
                0,
                2400,
                COMMAND_OUTPUT,
                2400,
                0,
                2400,
                90.0,
                2.4 if i == 0 else 0.0,
            )
            for i in range(len(prices))
        ]
        directive = plan_live_directive_from_plan(
            StrategyPlan(
                points,
                COMMAND_OUTPUT,
                2400,
                "reserve for later higher value",
                price_stats={"p_high": 40.0, "discharge_floor_ct": 36.0},
            ),
            StrategyOptions(
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=10,
                max_discharge_power_w=2400,
            ),
        )
        self.assertEqual(directive.discharge_budget_kwh, 2.4)

    def test_pv_surplus_charging_ignores_price_sensitive_grid_ceiling(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            20.0,
            500,
            1500,
            0,
            1000,
            -1000,
            COMMAND_INPUT,
            200,
            200,
            0,
            80.0,
        )
        inputs = StrategyInputs(
            grid_import_w=0,
            grid_export_w=1000,
            pv_w=1500,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=90,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
            discharge=DISCHARGE_PRICE_SENSITIVE,
            max_charge_power_w=2400,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan([point], COMMAND_INPUT, 200, "cheap grid slot")
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 1000)
        self.assertEqual(cmd.reason, "live_pv_surplus")

    def test_must_charge_uses_grid_only_for_gap_after_pv_surplus(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            18.2,
            500,
            700,
            3800,
            0,
            3800,
            COMMAND_INPUT,
            4000,
            4000,
            0,
            50.0,
        )
        inputs = StrategyInputs(
            grid_import_w=0,
            grid_export_w=200,
            pv_w=700,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=40,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
            discharge=DISCHARGE_PRICE_SENSITIVE,
            max_charge_power_w=4000,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan([point], COMMAND_INPUT, 4000, "cheap grid slot")
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 4000)
        self.assertEqual(cmd.reason, "must_charge")

    def test_must_charge_does_not_add_grid_when_pv_exceeds_required_rate(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            18.2,
            500,
            1200,
            3300,
            0,
            3300,
            COMMAND_INPUT,
            4000,
            4000,
            0,
            50.0,
        )
        inputs = StrategyInputs(
            grid_import_w=0,
            grid_export_w=4800,
            pv_w=5300,
            battery_power_w=0,
            ev_power_w=0,
            soc_pct=40,
        )
        options = StrategyOptions(
            pv_charging=PV_CHARGING_ON,
            grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
            discharge=DISCHARGE_PRICE_SENSITIVE,
            max_charge_power_w=5000,
        )
        diagnostics = calculate_command(inputs, options)
        plan = StrategyPlan([point], COMMAND_INPUT, 4000, "cheap grid slot")
        cmd = live_command_from_plan(plan, diagnostics, inputs, options)
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 4800)
        self.assertEqual(cmd.reason, "must_charge")

    def test_pv_surplus_charging_respects_pv_first_to_ev(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=0,
                grid_export_w=1000,
                pv_w=6000,
                battery_power_w=0,
                ev_power_w=4000,
                soc_pct=60,
            ),
            StrategyOptions(pv_charging=PV_CHARGING_ON, discharge=DISCHARGE_OFF),
        )
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 1000)
        self.assertEqual(cmd.pv_surplus_w, 1000)

    def test_pv_surplus_charging_never_treats_ev_load_as_export(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=0,
                grid_export_w=1000,
                pv_w=6000,
                battery_power_w=0,
                ev_power_w=4000,
                soc_pct=60,
            ),
            StrategyOptions(pv_charging=PV_CHARGING_ON, discharge=DISCHARGE_OFF),
        )
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 1000)
        self.assertEqual(cmd.pv_surplus_w, 1000)

    def test_live_discharge_budget_uses_current_soc_instead_of_stale_plan_soc(self):
        now = dt.datetime(2026, 7, 21, 20, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            40.0,
            500,
            0,
            500,
            0,
            500,
            COMMAND_IDLE,
            0,
            0,
            0,
            5.0,
            discharge_budget_kwh=0.6,
        )
        directive = plan_live_directive_from_plan(
            StrategyPlan([point], COMMAND_IDLE, 0, "cached plan soc"),
            StrategyOptions(discharge=DISCHARGE_PRICE_SENSITIVE, min_soc_pct=10),
            current_soc_pct=50.0,
        )
        self.assertEqual(directive.discharge_budget_kwh, 0.6)

    def test_live_discharge_budget_uses_configured_battery_capacity(self):
        now = dt.datetime(2026, 7, 21, 20, tzinfo=dt.timezone.utc)
        point = PlanPoint(
            int(now.timestamp() * 1000),
            now.date().isoformat(),
            40.0,
            500,
            0,
            500,
            0,
            500,
            COMMAND_IDLE,
            0,
            0,
            0,
            50.0,
            discharge_budget_kwh=5.0,
        )
        directive = plan_live_directive_from_plan(
            StrategyPlan([point], COMMAND_IDLE, 0, "capacity test"),
            StrategyOptions(
                discharge=DISCHARGE_PRICE_SENSITIVE,
                min_soc_pct=10,
                battery_capacity_kwh=10.0,
            ),
            current_soc_pct=50.0,
        )
        self.assertEqual(directive.discharge_budget_kwh, 4.0)

    def test_ev_power_uses_sensor_unit_instead_of_value_heuristic(self):
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator.entry = SimpleNamespace(data={"ev_power_entity": "sensor.ev_power"})
        state = SimpleNamespace(state="11", attributes={"unit_of_measurement": "kW"})
        coordinator.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: state))
        self.assertEqual(coordinator._ev_power_w(), 11000.0)
        state.attributes["unit_of_measurement"] = "W"
        self.assertEqual(coordinator._ev_power_w(), 11.0)

    def test_last_known_soc_is_loaded_for_restart_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "optimizer-state.json"
            path.write_text(json.dumps({"last_known_soc_pct": 37.0}), encoding="utf-8")
            self.assertEqual(_load_last_known_soc_pct(path), 37.0)

    def test_last_sample_soc_bridges_upgrade_before_persisted_soc_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "optimizer-state.json"
            path.write_text(
                json.dumps({"samples": [{"soc": 42.0}, {"soc": -1}]}),
                encoding="utf-8",
            )
            self.assertEqual(_load_last_known_soc_pct(path), 42.0)

    def test_coordinator_uses_last_known_soc_while_entity_is_unavailable(self):
        coordinator = object.__new__(BatteryStrategyCoordinator)
        coordinator.entry = SimpleNamespace(
            data={"battery_soc_entity": "sensor.battery_soc"}
        )
        coordinator.hass = SimpleNamespace(
            states=SimpleNamespace(
                get=lambda _entity_id: SimpleNamespace(state="unavailable")
            )
        )
        coordinator._last_known_soc_pct = 37.0
        self.assertEqual(coordinator._battery_soc_pct(), 37.0)

    def test_manual_charge_is_override_but_respects_battery_limits(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=0,
                grid_export_w=0,
                pv_w=0,
                battery_power_w=0,
                ev_power_w=0,
                soc_pct=80,
            ),
            StrategyOptions(
                manual_mode=MANUAL_CHARGE,
                manual_power_w=3000,
                max_charge_power_w=2400,
                pv_charging=PV_CHARGING_OFF,
                discharge=DISCHARGE_OFF,
            ),
        )
        self.assertEqual(cmd.mode, COMMAND_INPUT)
        self.assertEqual(cmd.power_w, 2400)
        self.assertEqual(cmd.reason, "manual_charge")

    def test_manual_charge_stops_at_max_soc(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=0,
                grid_export_w=0,
                pv_w=0,
                battery_power_w=0,
                soc_pct=100,
            ),
            StrategyOptions(
                manual_mode=MANUAL_CHARGE, manual_power_w=1000, max_soc_pct=100
            ),
        )
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.reason, "max_soc")

    def test_manual_discharge_is_override_and_may_feed_ev(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=100,
                grid_export_w=0,
                pv_w=0,
                battery_power_w=0,
                ev_power_w=5000,
                soc_pct=80,
            ),
            StrategyOptions(
                manual_mode=MANUAL_DISCHARGE,
                manual_power_w=2000,
                battery_may_feed_ev=False,
                max_discharge_power_w=2400,
            ),
        )
        self.assertEqual(cmd.mode, COMMAND_OUTPUT)
        self.assertEqual(cmd.power_w, 2000)
        self.assertEqual(cmd.reason, "manual_discharge")

    def test_manual_discharge_stops_at_min_soc(self):
        cmd = calculate_command(
            StrategyInputs(
                grid_import_w=1000,
                grid_export_w=0,
                pv_w=0,
                battery_power_w=0,
                soc_pct=10,
            ),
            StrategyOptions(
                manual_mode=MANUAL_DISCHARGE, manual_power_w=1000, min_soc_pct=10
            ),
        )
        self.assertEqual(cmd.mode, COMMAND_IDLE)
        self.assertEqual(cmd.reason, "min_soc")

    def test_zendure_targets_clear_opposite_limit(self):
        charge = calculate_command(
            StrategyInputs(
                grid_import_w=0,
                grid_export_w=500,
                pv_w=500,
                battery_power_w=0,
                soc_pct=80,
            ),
            StrategyOptions(discharge=DISCHARGE_OFF),
        )
        targets = zendure_targets(charge)
        self.assertEqual(targets.mode_option, "Input mode")
        self.assertEqual(targets.input_limit_w, 500)
        self.assertEqual(targets.output_limit_w, 0)

    def test_limit_write_respects_delta_and_min_interval(self):
        options = StrategyOptions(min_command_delta_w=20)
        self.assertFalse(should_write_limit(500, 510, 30, options))
        self.assertFalse(should_write_limit(500, 600, 10, options))
        self.assertTrue(should_write_limit(500, 600, 30, options))
        self.assertTrue(should_write_limit(10, 0, 30, options, force_zero=True))

    def test_mode_write_respects_min_interval_and_current_mode(self):
        self.assertFalse(should_write_mode("Input mode", "Input mode", 30))
        self.assertFalse(should_write_mode("input", "Input mode", 30))
        self.assertFalse(should_write_mode("Output mode", "Input mode", 10))
        self.assertTrue(should_write_mode("Output mode", "Input mode", 30))

    def test_tibber_prices_storage_reader_accepts_eur_and_ct_values(self):
        now = dt.datetime(2026, 5, 26, 10, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tibber_prices.interval_pool.test"
            path.write_text(
                json.dumps(
                    {
                        "data": {
                            "fetch_groups": [
                                {
                                    "intervals": [
                                        {
                                            "startsAt": "2026-05-26T10:00:00+00:00",
                                            "total": 0.31,
                                        },
                                        {
                                            "start": "2026-05-26T11:00:00+00:00",
                                            "total": 28.0,
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            points = read_tibber_price_points(
                str(Path(tmp) / "tibber_prices.interval_pool.*"), now, 3
            )
        self.assertEqual([round(p.price_ct, 1) for p in points], [31.0, 28.0])

    def test_eex_proxy_fills_missing_tomorrow_prices(self):
        tz = dt.timezone(dt.timedelta(hours=2))
        today = dt.date(2026, 6, 11)
        tomorrow = today + dt.timedelta(days=1)
        intervals = []
        for day_offset, avg in ((-2, 30.0), (-1, 32.0), (0, 31.0)):
            day = today + dt.timedelta(days=day_offset)
            for slot in range(96):
                ts = dt.datetime.combine(day, dt.time.min, tzinfo=tz) + dt.timedelta(
                    minutes=15 * slot
                )
                hour = slot // 4
                shape = 7.0 if 18 <= hour < 21 else -6.0 if 12 <= hour < 15 else 0.0
                intervals.append(
                    {
                        "dt": ts,
                        "ts": ts.isoformat(),
                        "price_eur": (avg + shape) / 100.0,
                        "source": "tibber",
                    }
                )
        eex_days = {
            today.isoformat(): {
                "base": {"settl_ct_kwh": 11.0},
                "peak": {"settl_ct_kwh": 9.0},
            },
            tomorrow.isoformat(): {
                "base": {"settl_ct_kwh": 9.0},
                "peak": {"settl_ct_kwh": 8.0},
            },
        }

        filled, source = optimizer_engine.apply_eex_proxy_prices(
            intervals, eex_days, today, tomorrow
        )

        tomorrow_prices = [it for it in filled if it["dt"].date() == tomorrow]
        self.assertEqual(source, "eex_proxy")
        self.assertEqual(len(tomorrow_prices), 96)
        self.assertTrue(all(it["source"] == "eex_proxy" for it in tomorrow_prices))
        noon = [
            it["price_eur"] * 100 for it in tomorrow_prices if 12 <= it["dt"].hour < 15
        ]
        evening = [
            it["price_eur"] * 100 for it in tomorrow_prices if 18 <= it["dt"].hour < 21
        ]
        self.assertLess(sum(noon) / len(noon), sum(evening) / len(evening))

    def test_eex_proxy_does_not_replace_real_tomorrow_prices(self):
        tz = dt.timezone.utc
        today = dt.date(2026, 6, 11)
        tomorrow = today + dt.timedelta(days=1)
        intervals = []
        for slot in range(96):
            ts = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=tz) + dt.timedelta(
                minutes=15 * slot
            )
            intervals.append(
                {"dt": ts, "ts": ts.isoformat(), "price_eur": 0.25, "source": "tibber"}
            )

        filled, source = optimizer_engine.apply_eex_proxy_prices(
            intervals, {}, today, tomorrow
        )

        self.assertEqual(source, "tibber")
        self.assertEqual(len([it for it in filled if it["dt"].date() == tomorrow]), 96)
        self.assertTrue(all(it["source"] == "tibber" for it in filled))

    def test_forecast_fallbacks_are_bounded(self):
        self.assertEqual(clamp_bias(2.0, 0.5, 1.4), 1.4)
        self.assertGreater(fallback_weather_factor(None, None), 0.0)
        self.assertLessEqual(fallback_weather_factor(100, None), 1.0)

    def test_full_optimizer_does_not_plan_discharge_export_when_feed_in_is_zero(self):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 5, 26, 18, tzinfo=tz)
        intervals = [
            {
                "dt": start + dt.timedelta(minutes=15 * i),
                "price_eur": (80.0 if i < 4 else 20.0) / 100.0,
            }
            for i in range(8)
        ]
        samples = [
            {
                "ts": (
                    start - dt.timedelta(days=7) + dt.timedelta(minutes=15 * i)
                ).timestamp(),
                "load_w": 500.0,
                "house_w": 500.0,
                "house_total_w": 500.0,
                "wallbox_w": 0.0,
                "grid_import_w": 0.0,
                "grid_export_w": 0.0,
                "pv_w": 0.0,
                "hp_w": 0.0,
                "price_ct": 30.0,
            }
            for i in range(8)
        ]
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=optimizer_engine.MAX_E_KWH,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )
        for point in plan["points"]:
            load_w = float(point["load_fc_w"])
            pv_w = float(point["pv_fc_w"])
            discharge_w = float(point["discharge_fc_w"])
            self.assertLessEqual(discharge_w, max(0.0, load_w - pv_w) + 1.0)

    def test_optimizer_discharge_budget_opens_when_future_pv_would_spill(self):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 5, 29, 10, tzinfo=tz)
        intervals = [
            {
                "dt": start + dt.timedelta(minutes=15 * i),
                "price_eur": (25.0 if i == 0 else 40.0) / 100.0,
            }
            for i in range(8)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i in range(8):
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": 500.0 if i == 0 else 100.0,
                        "house_w": 500.0 if i == 0 else 100.0,
                        "house_total_w": 500.0 if i == 0 else 100.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 0.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0 if i == 0 else 3000.0,
                        "hp_w": 0.0,
                        "price_ct": 25.0,
                    }
                )
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=5.0,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )
        self.assertGreater(plan["points"][0]["discharge_budget_kwh"], 0.0)

    def test_optimizer_discharge_budget_opens_for_planned_future_export(self):
        tz = optimizer_engine.OPEN_METEO_TZ
        start = dt.datetime(2026, 6, 23, 12, tzinfo=tz)
        prices = [27.0, 26.0, 25.5, 25.0, 28.0, 35.0]
        intervals = [
            {"dt": start + dt.timedelta(minutes=15 * i), "price_eur": price / 100.0}
            for i, price in enumerate(prices)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i in range(len(prices)):
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": 800.0 if i == 0 else 100.0,
                        "house_w": 800.0 if i == 0 else 100.0,
                        "house_total_w": 800.0 if i == 0 else 100.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 800.0 if i == 0 else 0.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0 if i == 0 else 3000.0,
                        "hp_w": 0.0,
                        "price_ct": 30.0,
                    }
                )
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=3.0,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )

        self.assertGreater(sum(p["grid_export_fc_w"] for p in plan["points"][1:4]), 0.0)
        self.assertGreater(plan["points"][0]["discharge_budget_kwh"], 0.0)

    def test_optimizer_discharge_budget_reserves_scarce_energy_for_later_high_prices(
        self,
    ):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 5, 29, 10, tzinfo=tz)
        intervals = [
            {
                "dt": start + dt.timedelta(minutes=15 * i),
                "price_eur": (25.0 if i == 0 else 40.0) / 100.0,
            }
            for i in range(8)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i in range(8):
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": 500.0,
                        "house_w": 500.0,
                        "house_total_w": 500.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 500.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0,
                        "hp_w": 0.0,
                        "price_ct": 30.0,
                    }
                )
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=2.0,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )
        self.assertEqual(plan["points"][0]["discharge_budget_kwh"], 0.0)
        self.assertGreater(
            max(p["discharge_budget_kwh"] for p in plan["points"][1:]), 0.0
        )

    def test_optimizer_discharge_budget_does_not_reserve_across_future_charge_window(
        self,
    ):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 7, 13, 19, tzinfo=tz)
        prices = [36.0, 36.4, 38.0, 41.0, 40.0, 39.0, 20.0] + [41.0] * 16
        intervals = [
            {"dt": start + dt.timedelta(minutes=15 * i), "price_eur": price / 100.0}
            for i, price in enumerate(prices)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i, _price in enumerate(prices):
                load_w = 300.0 if i < 6 else 2400.0
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": load_w,
                        "house_w": load_w,
                        "house_total_w": load_w,
                        "wallbox_w": 0.0,
                        "grid_import_w": load_w,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0,
                        "hp_w": 0.0,
                        "price_ct": 30.0,
                    }
                )

        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=2.0,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )

        self.assertGreater(plan["points"][0]["discharge_budget_kwh"], 0.0)
        self.assertGreater(plan["points"][1]["discharge_budget_kwh"], 0.0)
        self.assertGreater(plan["points"][6]["charge_fc_w"], 0.0)

    def test_optimizer_discharge_floor_uses_cheapest_horizon_replacement(self):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 6, 21, 0, tzinfo=tz)
        prices = [
            35.83,
            34.71,
            34.15,
            33.75,
            33.0,
            31.0,
            28.0,
            24.0,
            18.2,
            18.2,
            18.2,
            18.2,
            35.0,
            36.0,
            35.0,
            34.0,
        ]
        intervals = [
            {"dt": start + dt.timedelta(minutes=15 * i), "price_eur": price / 100.0}
            for i, price in enumerate(prices)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i in range(len(prices)):
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": 300.0,
                        "house_w": 300.0,
                        "house_total_w": 300.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 300.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0,
                        "hp_w": 0.0,
                        "price_ct": 30.0,
                    }
                )
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=4.2,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )

        self.assertAlmostEqual(
            plan["price_stats"]["discharge_floor_ct"],
            (18.2 / optimizer_engine.ETA_RT) + optimizer_engine.MIN_MARGIN_CT,
            places=3,
        )
        self.assertGreater(plan["points"][3]["discharge_budget_kwh"], 0.0)

    def test_optimizer_discharge_budget_opens_when_no_later_higher_price_reserve_is_needed(
        self,
    ):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 5, 29, 21, tzinfo=tz)
        prices = [35.0, 32.0, 29.0, 26.0, 23.0, 20.0]
        intervals = [
            {"dt": start + dt.timedelta(minutes=15 * i), "price_eur": price / 100.0}
            for i, price in enumerate(prices)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i in range(len(prices)):
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": 2400.0,
                        "house_w": 2400.0,
                        "house_total_w": 2400.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 2400.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0,
                        "hp_w": 0.0,
                        "price_ct": 30.0,
                    }
                )
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=1.0,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )
        budgets = [point["discharge_budget_kwh"] for point in plan["points"][:4]]
        self.assertGreater(budgets[0], 0.0)
        self.assertGreater(budgets[1], 0.0)
        self.assertGreater(budgets[2], 0.0)

    def test_optimizer_discharge_budget_does_not_reserve_for_equal_value_later_slots(
        self,
    ):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 5, 29, 21, tzinfo=tz)
        prices = [35.0, 35.0, 35.0, 35.0]
        intervals = [
            {"dt": start + dt.timedelta(minutes=15 * i), "price_eur": price / 100.0}
            for i, price in enumerate(prices)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i in range(len(prices)):
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": 2400.0,
                        "house_w": 2400.0,
                        "house_total_w": 2400.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 2400.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0,
                        "hp_w": 0.0,
                        "price_ct": 30.0,
                    }
                )
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=1.0,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )
        budgets = [point["discharge_budget_kwh"] for point in plan["points"][:4]]
        self.assertGreater(budgets[0], 0.0)
        self.assertGreater(budgets[1], 0.0)

    def test_optimizer_discharge_budget_peak_slot_is_not_capped_by_current_forecast_load(
        self,
    ):
        tz = dt.timezone.utc
        start = dt.datetime(2026, 7, 16, 20, 30, tzinfo=tz)
        prices = [58.0, 54.0, 50.0, 45.0]
        intervals = [
            {"dt": start + dt.timedelta(minutes=15 * i), "price_eur": price / 100.0}
            for i, price in enumerate(prices)
        ]
        samples = []
        for weeks_ago in range(1, 5):
            base = start - dt.timedelta(days=7 * weeks_ago)
            for i in range(len(prices)):
                samples.append(
                    {
                        "ts": (base + dt.timedelta(minutes=15 * i)).timestamp(),
                        "load_w": 200.0,
                        "house_w": 200.0,
                        "house_total_w": 200.0,
                        "wallbox_w": 0.0,
                        "grid_import_w": 200.0,
                        "grid_export_w": 0.0,
                        "pv_w": 0.0,
                        "hp_w": 0.0,
                        "price_ct": 40.0,
                    }
                )
        plan = optimizer_engine.build_virtual_plan(
            intervals=intervals,
            samples=samples,
            start_energy_kwh=3.0,
            weather_factor=1.0,
            forecast_tomorrow_kwh=None,
            load_bias=1.0,
            load_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            pv_bias_slots=[1.0] * optimizer_engine.SLOTS_PER_DAY,
            initial_mode=0,
            weather_hourly={},
            pv_now_actual_w=0.0,
            now_local=start,
            pv_global_bias=1.0,
            eex_days={},
        )

        self.assertEqual(
            plan["points"][0]["discharge_budget_kwh"],
            round(optimizer_engine.MAX_E_SLOT_KWH, 3),
        )

    def test_optimizer_discharge_budget_has_no_current_charge_path_hard_block(self):
        source = Path(optimizer_engine.__file__).read_text(encoding="utf-8")
        budget_fn = source.split("def explicit_discharge_budget_kwh", 1)[1].split(
            "for idx, point in enumerate(points)", 1
        )[0]
        self.assertNotIn("path_charge_in[t]", budget_fn)

    def test_optimizer_adapter_filters_expired_cached_slots(self):
        now = dt.datetime(2026, 5, 29, 12, tzinfo=dt.timezone.utc)
        old_ts = int((now - dt.timedelta(minutes=20)).timestamp() * 1000)
        current_ts = int(now.timestamp() * 1000)
        output = {
            "profile_48h_price": [[old_ts, 30.0], [current_ts, 31.0]],
            "profile_today_soc": [[old_ts, 50.0], [current_ts, 49.0]],
            "profile_today_power": [[old_ts, 100.0], [current_ts, 200.0]],
            "profile_48h_charge_fc_power": [[old_ts, 0.0], [current_ts, 0.0]],
            "profile_48h_discharge_fc_power": [[old_ts, 100.0], [current_ts, 200.0]],
            "profile_48h_discharge_budget_kwh": [[old_ts, 0.025], [current_ts, 0.05]],
            "profile_48h_pv_fc_power": [[old_ts, 0.0], [current_ts, 0.0]],
            "profile_48h_house_fc_power": [[old_ts, 100.0], [current_ts, 200.0]],
            "profile_48h_grid_import_fc_power": [[old_ts, 0.0], [current_ts, 0.0]],
            "profile_48h_grid_export_fc_power": [[old_ts, 0.0], [current_ts, 0.0]],
            "profile_48h_grid_net_fc_power": [[old_ts, 0.0], [current_ts, 0.0]],
        }
        points = optimizer_adapter._points_from_output(output, now_ms=current_ts)
        self.assertEqual([point.ts_ms for point in points], [current_ts])

    def test_optimizer_adapter_assigns_dates_in_home_assistant_timezone(self):
        ts = int(
            dt.datetime(2026, 5, 29, 22, 0, tzinfo=dt.timezone.utc).timestamp() * 1000
        )
        output = {
            "profile_48h_price": [[ts, 30.0]],
            "profile_48h_house_fc_power": [[ts, 200.0]],
        }
        points = optimizer_adapter._points_from_output(
            output,
            now_ms=ts,
            timezone=ZoneInfo("Europe/Berlin"),
        )
        self.assertEqual(points[0].date, "2026-05-30")

    def test_live_overlay_does_not_turn_display_plan_into_battery_export(self):
        now = dt.datetime(2026, 5, 26, 18, tzinfo=dt.timezone.utc)
        future = [
            {
                "ts_ms": int((now + dt.timedelta(minutes=15)).timestamp() * 1000),
                "load_fc_w": 700.0,
                "pv_fc_w": 500.0,
                "power_w": 0.0,
                "charge_fc_w": 0.0,
                "discharge_fc_w": 0.0,
                "grid_import_fc_w": 200.0,
                "grid_export_fc_w": 0.0,
                "grid_net_fc_w": 200.0,
                "mode": "idle",
                "soc_pct": 80.0,
            }
        ]
        out = optimizer_engine.apply_live_override_to_future_points(
            future,
            "discharge_limited",
            2400,
            int(now.timestamp() * 1000),
        )
        self.assertEqual(out[0]["discharge_fc_w"], 200.0)
        self.assertEqual(out[0]["grid_export_fc_w"], 0.0)


if __name__ == "__main__":
    unittest.main()

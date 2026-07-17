"""Data coordinator for Battery Strategy read-only parallel operation."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .actuator import should_write_limit, should_write_mode, zendure_targets
from .const import (
    BATTERY_PROFILE_ZENDURE,
    COMMAND_IDLE,
    COMMAND_INPUT,
    COMMAND_OUTPUT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_PROFILE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_GRID_L1_ENTITY,
    CONF_GRID_L2_ENTITY,
    CONF_GRID_L3_ENTITY,
    CONF_GRID_MODE,
    CONF_REFERENCE_MODE_ENTITY,
    CONF_REFERENCE_OUTPUT_ENTITY,
    CONF_REFERENCE_POWER_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SIGNED_GRID_POWER_ENTITY,
    CONF_ZENDURE_AC_MODE_ENTITY,
    CONF_ZENDURE_GRID_INPUT_POWER_ENTITY,
    CONF_ZENDURE_INPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY,
    CONF_ZENDURE_OUTPUT_LIMIT_ENTITY,
    CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY,
    CONF_ZENDURE_PACK_INPUT_POWER_ENTITY,
    DISCHARGE_LOAD,
    DOMAIN,
    GRID_CHARGING_OFF,
    GRID_MODE_IMPORT_EXPORT,
    GRID_MODE_SIGNED,
    GRID_MODE_THREE_PHASE,
    MANUAL_OFF,
    PV_CHARGING_ON,
)
from .models import StrategyCommand, StrategyInputs, StrategyOptions
from .optimizer import build_optimizer_plan
from .parallel import ParallelEvaluation, compare_optimizer_plan, evaluate_parallel_commands
from .plan_models import PlanComparison, PlanLiveDirective, PricePoint, StrategyPlan
from .pricing import price_points_from_profile, read_tibber_price_points
from .optimizer_adapter import OptimizerEngineAdapter
from .history import write_json_atomic
from .strategy import calculate_command, live_command_from_directive, plan_live_directive_from_plan

LOGGER = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("Europe/Berlin")
OPTIMIZER_PREFETCH_LEAD_S = 60


class BatteryStrategyCoordinator(DataUpdateCoordinator):
    """Collect HA states and calculate read-only strategy commands."""

    def __init__(self, hass, entry, update_interval):
        """Initialize coordinator."""
        super().__init__(
            hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.entry = entry
        self._manual_mode = MANUAL_OFF
        self._manual_power_w = 0.0
        self._manual_until: dt.datetime | None = None
        self._new_commands: list[StrategyCommand] = []
        self._reference_modes: list[str] = []
        self._reference_powers_w: list[float] = []
        self._new_data_points: list[dict[str, float]] = []
        self._reference_input_points: list[dict[str, float]] = []
        self.parallel_evaluation = ParallelEvaluation(0, 0, 0)
        self.plan_comparison = PlanComparison(False, False, False, False, False, 0, 0, 0, 0, 0.0, 0, 0)
        self._optimizer_engine = OptimizerEngineAdapter()
        self._optimizer_attrs: dict = {}
        self.last_actuation: dict[str, object] = {"status": "not_started"}
        self._active_directive_slot_id: str | None = None
        self._active_directive_slot_end_ts_ms: int = 0
        self._last_optimizer_force_key: str | None = None
        self._slot_charged_kwh = 0.0
        self._slot_discharged_kwh = 0.0
        self._active_discharge_budget_base_kwh = 0.0
        self._active_discharge_mode: str | None = None
        self._last_live_accounting_ts: dt.datetime | None = None
        self._last_live_command: StrategyCommand | None = None
        self._strategy_was_enabled = bool(entry.options.get("strategy_enabled", True))
        self._disabled_zeroed = not self._strategy_was_enabled

    def set_manual_override(self, mode: str, power_w: float, duration_min: int = 0) -> None:
        """Set an in-memory manual override."""
        self._manual_mode = mode
        self._manual_power_w = max(0.0, float(power_w))
        if duration_min > 0:
            self._manual_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=duration_min)
        else:
            self._manual_until = None

    def clear_manual_override(self) -> None:
        """Clear manual override."""
        self._manual_mode = MANUAL_OFF
        self._manual_power_w = 0.0
        self._manual_until = None

    def reset_parallel_samples(self) -> None:
        """Clear parallel comparison samples."""
        self._new_commands.clear()
        self._reference_modes.clear()
        self._reference_powers_w.clear()
        self._new_data_points.clear()
        self._reference_input_points.clear()
        self.parallel_evaluation = ParallelEvaluation(0, 0, 0)

    def _account_last_live_command(self, now: dt.datetime) -> None:
        """Account energy used by the last live command inside the active slot."""
        if self._last_live_accounting_ts is None or self._last_live_command is None:
            return
        elapsed_h = max(0.0, (now - self._last_live_accounting_ts).total_seconds()) / 3600.0
        if elapsed_h <= 0.0 or elapsed_h > 0.25:
            return
        energy_kwh = float(self._last_live_command.power_w) * elapsed_h / 1000.0
        if self._last_live_command.mode == COMMAND_INPUT:
            self._slot_charged_kwh += energy_kwh
        elif self._last_live_command.mode == COMMAND_OUTPUT:
            self._slot_discharged_kwh += energy_kwh

    def _directive_with_progress(
        self,
        directive: PlanLiveDirective,
        *,
        discharge_mode: str | None = None,
        allow_discharge_budget_increase: bool = False,
    ) -> PlanLiveDirective:
        """Return directive with slot-local charge/discharge budgets decremented."""
        slot_changed = directive.slot_id != self._active_directive_slot_id
        active_mode = getattr(self, "_active_discharge_mode", None)
        mode_changed = discharge_mode is not None and discharge_mode != active_mode
        if slot_changed or mode_changed:
            self._active_directive_slot_id = directive.slot_id
            self._active_directive_slot_end_ts_ms = int(directive.slot_end_ts)
            if slot_changed:
                self._slot_charged_kwh = 0.0
                self._slot_discharged_kwh = 0.0
            self._active_discharge_mode = discharge_mode
            self._active_discharge_budget_base_kwh = max(0.0, float(directive.discharge_budget_kwh))
        else:
            current_base = max(0.0, float(getattr(self, "_active_discharge_budget_base_kwh", 0.0)))
            new_base = max(0.0, float(directive.discharge_budget_kwh))
            if allow_discharge_budget_increase:
                self._active_discharge_budget_base_kwh = max(current_base, new_base)
            else:
                self._active_discharge_budget_base_kwh = min(current_base, new_base)
        return replace(
            directive,
            must_charge_remaining_kwh=round(max(0.0, directive.must_charge_remaining_kwh - self._slot_charged_kwh), 3),
            discharge_budget_kwh=round(
                max(0.0, self._active_discharge_budget_base_kwh - self._slot_discharged_kwh),
                3,
            ),
        )

    async def _async_update_data(self):
        """Fetch current states and calculate command."""
        if self._manual_until is not None and dt.datetime.now(dt.timezone.utc) >= self._manual_until:
            self.clear_manual_override()

        options = self._strategy_options()
        inputs = self._strategy_inputs()
        now = dt.datetime.now(dt.timezone.utc)
        self._account_last_live_command(now)
        force_optimizer = self._should_force_optimizer(now)
        simple_command = calculate_command(inputs, options)
        plan, self._optimizer_attrs = await self.hass.async_add_executor_job(
            self._optimizer_engine.run,
            inputs,
            options,
            force_optimizer,
        )
        directive = self._directive_with_progress(
            plan_live_directive_from_plan(plan, options),
            discharge_mode=options.discharge,
            allow_discharge_budget_increase=options.discharge == DISCHARGE_LOAD,
        )
        command = live_command_from_directive(directive, simple_command, inputs, options)
        calculated_command = command
        strategy_enabled = bool(self.entry.options.get("strategy_enabled", True))
        display_command = command if strategy_enabled else self._disabled_display_command(command)
        self._last_live_command = display_command
        self._last_live_accounting_ts = now
        reference_plan_attrs = await self.hass.async_add_executor_job(self._reference_plan_attrs_sync)
        self.plan_comparison = self._compare_optimizer_plan(plan, command, reference_plan_attrs)
        self._record_parallel_sample(inputs, command)
        data = {
            "inputs": inputs,
            "options": options,
            "command": display_command,
            "calculated_command": calculated_command,
            "plan": plan,
            "optimizer_attrs": self._optimizer_attrs,
            "plan_to_live": directive,
            "plan_comparison": self.plan_comparison,
            "parallel": self.parallel_evaluation,
            "send_commands": strategy_enabled,
            "strategy_enabled": strategy_enabled,
            "actuation": self.last_actuation,
            "optimizer_age_s": self._optimizer_engine.age_s(),
            "optimizer_forced": force_optimizer,
        }
        if strategy_enabled:
            self._strategy_was_enabled = True
            self._disabled_zeroed = False
            await self._async_apply_command(calculated_command, options)
            data["actuation"] = self.last_actuation
        elif self._strategy_was_enabled and not self._disabled_zeroed:
            await self._async_zero_limits_once()
            self._strategy_was_enabled = False
            self._disabled_zeroed = True
            data["actuation"] = self.last_actuation
        else:
            self._strategy_was_enabled = False
            self.last_actuation = {"status": "disabled_no_write", "reason": "strategy_disabled"}
            data["actuation"] = self.last_actuation
        await self.hass.async_add_executor_job(self._write_parallel_state, data)
        if bool(self.entry.options.get("trace_enabled", False)):
            await self.hass.async_add_executor_job(self._append_command_trace, data)
        self._publish_parallel_dashboard_states(data)
        return data

    def _should_force_optimizer(self, now: dt.datetime) -> bool:
        """Return whether the optimizer should refresh around the current slot boundary."""
        if self._active_directive_slot_end_ts_ms <= 0:
            return False
        now_ms = int(now.timestamp() * 1000)
        lead_ms = OPTIMIZER_PREFETCH_LEAD_S * 1000
        if now_ms < self._active_directive_slot_end_ts_ms - lead_ms:
            return False
        phase = "expired" if now_ms >= self._active_directive_slot_end_ts_ms else "prefetch"
        key = f"{self._active_directive_slot_id}:{self._active_directive_slot_end_ts_ms}:{phase}"
        if key == self._last_optimizer_force_key:
            return False
        self._last_optimizer_force_key = key
        return True

    def _strategy_options(self) -> StrategyOptions:
        opts = dict(self.entry.options)
        manual_mode = self._manual_mode if self._manual_mode != MANUAL_OFF else opts.get("manual_mode", MANUAL_OFF)
        manual_power = self._manual_power_w if self._manual_mode != MANUAL_OFF else float(opts.get("manual_power_w", 0.0))
        return StrategyOptions(
            pv_charging=opts.get("pv_charging", PV_CHARGING_ON),
            grid_charging=opts.get("grid_charging", GRID_CHARGING_OFF),
            discharge=opts.get("discharge", DISCHARGE_LOAD),
            pv_to_ev_first=bool(opts.get("pv_to_ev_first", True)),
            battery_may_feed_ev=bool(opts.get("battery_may_feed_ev", False)),
            ev_active_threshold_w=float(opts.get("ev_active_threshold_w", 300.0)),
            min_soc_pct=float(opts.get("min_soc_pct", 10.0)),
            max_soc_pct=float(opts.get("max_soc_pct", 100.0)),
            max_charge_power_w=float(opts.get("max_charge_power_w", 2400.0)),
            max_discharge_power_w=float(opts.get("max_discharge_power_w", 2400.0)),
            min_command_power_w=float(opts.get("min_command_power_w", 20.0)),
            min_command_delta_w=float(opts.get("min_command_delta_w", 20.0)),
            round_trip_efficiency=float(opts.get("round_trip_efficiency", 0.80)),
            min_margin_ct_per_kwh=float(opts.get("min_margin_ct_per_kwh", 2.0)),
            planning_horizon_h=int(opts.get("planning_horizon_h", 48)),
            feed_in_tariff_ct_per_kwh=float(opts.get("feed_in_tariff_ct_per_kwh", 0.0)),
            manual_mode=manual_mode,
            manual_power_w=manual_power,
        )

    def _effective_send_commands(self) -> bool:
        """Return whether Battery Strategy is allowed to write commands."""
        return bool(self.entry.options.get("strategy_enabled", True))

    def _strategy_inputs(self) -> StrategyInputs:
        grid_import, grid_export = self._grid_import_export()
        return StrategyInputs(
            grid_import_w=grid_import,
            grid_export_w=grid_export,
            pv_w=self._state_float(CONF_PV_POWER_ENTITY),
            battery_power_w=self._battery_power_w(),
            ev_power_w=self._ev_power_w(),
            soc_pct=self._state_float(CONF_BATTERY_SOC_ENTITY, 50.0),
        )

    def _grid_import_export(self) -> tuple[float, float]:
        mode = self.entry.data.get(CONF_GRID_MODE, GRID_MODE_THREE_PHASE)
        if mode == GRID_MODE_SIGNED:
            net = self._state_float(CONF_SIGNED_GRID_POWER_ENTITY)
            return max(0.0, net), max(0.0, -net)
        if mode == GRID_MODE_IMPORT_EXPORT:
            return self._state_float(CONF_GRID_IMPORT_ENTITY), self._state_float(CONF_GRID_EXPORT_ENTITY)
        if mode == GRID_MODE_THREE_PHASE:
            net = (
                self._state_float(CONF_GRID_L1_ENTITY)
                + self._state_float(CONF_GRID_L2_ENTITY)
                + self._state_float(CONF_GRID_L3_ENTITY)
            )
            return max(0.0, net), max(0.0, -net)
        return 0.0, 0.0

    def _battery_power_w(self) -> float:
        if self.entry.data.get(CONF_BATTERY_PROFILE) != BATTERY_PROFILE_ZENDURE:
            return self._state_float(CONF_BATTERY_POWER_ENTITY)

        ac_mode = self._state_value(CONF_ZENDURE_AC_MODE_ENTITY).lower()
        output_pack = self._state_float(CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY)
        pack_input = self._state_float(CONF_ZENDURE_PACK_INPUT_POWER_ENTITY)
        output_home = self._state_float(CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY)
        grid_input = self._state_float(CONF_ZENDURE_GRID_INPUT_POWER_ENTITY)

        if "input" in ac_mode:
            return -max(grid_input, output_pack, pack_input, output_home, 0.0)
        if "output" in ac_mode:
            return max(output_home, pack_input, grid_input, 0.0)
        return 0.0

    def _ev_power_w(self) -> float:
        entity_id = self.entry.data.get(CONF_EV_POWER_ENTITY)
        if not entity_id:
            return 0.0
        raw = self._raw_state_float(entity_id)
        # Current wallbox source reports kW. Treat small values as kW for compatibility.
        return raw * 1000.0 if 0.0 < raw < 50.0 else raw

    def _state_float(self, config_key: str, default: float = 0.0) -> float:
        entity_id = self.entry.data.get(config_key)
        if not entity_id:
            return default
        return self._raw_state_float(entity_id, default)

    def _raw_state_float(self, entity_id: str, default: float = 0.0) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "none", ""):
            return default
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return default

    def _state_value(self, config_key: str) -> str:
        entity_id = self.entry.data.get(config_key)
        if not entity_id:
            return ""
        state = self.hass.states.get(entity_id)
        return "" if state is None else str(state.state)

    def _entity_id(self, config_key: str, default: str = "") -> str:
        """Return configured entity id with a default for existing imports."""
        return self.entry.data.get(config_key) or default

    def _state_age_s(self, entity_id: str) -> float:
        """Return seconds since a state changed."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return 1e9
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - state.last_changed).total_seconds())

    def _state_available(self, entity_id: str) -> bool:
        """Return whether an entity has a usable state."""
        state = self.hass.states.get(entity_id)
        return state is not None and state.state not in ("unknown", "unavailable", "none", "")

    def _grid_inputs_fresh(self) -> bool:
        """Return whether live grid inputs are fresh enough for real actuation."""
        mode = self.entry.data.get(CONF_GRID_MODE, GRID_MODE_THREE_PHASE)
        if mode == GRID_MODE_THREE_PHASE:
            entities = [
                self.entry.data.get(CONF_GRID_L1_ENTITY),
                self.entry.data.get(CONF_GRID_L2_ENTITY),
                self.entry.data.get(CONF_GRID_L3_ENTITY),
            ]
            return all(entity and self._state_available(entity) and self._state_age_s(entity) < 180 for entity in entities)
        if mode == GRID_MODE_SIGNED:
            entity = self.entry.data.get(CONF_SIGNED_GRID_POWER_ENTITY)
            return bool(entity and self._state_available(entity) and self._state_age_s(entity) < 180)
        if mode == GRID_MODE_IMPORT_EXPORT:
            entities = [self.entry.data.get(CONF_GRID_IMPORT_ENTITY), self.entry.data.get(CONF_GRID_EXPORT_ENTITY)]
            return all(entity and self._state_available(entity) and self._state_age_s(entity) < 180 for entity in entities)
        return False


    async def _async_zero_limits_once(self) -> None:
        """Stop charging/discharging once when control is disabled, then stay hands-off."""
        input_limit_entity = self._entity_id(CONF_ZENDURE_INPUT_LIMIT_ENTITY, "number.hoa1nan7n331666_inputlimit")
        output_limit_entity = self._entity_id(CONF_ZENDURE_OUTPUT_LIMIT_ENTITY, "number.hoa1nan7n331666_outputlimit")
        required_entities = [input_limit_entity, output_limit_entity]
        if not all(self._state_available(entity) for entity in required_entities):
            self.last_actuation = {"status": "skipped", "reason": "control_entity_unavailable"}
            return

        current_input = self._raw_state_float(input_limit_entity)
        current_output = self._raw_state_float(output_limit_entity)
        actions: list[str] = []
        if current_input > 0:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": input_limit_entity, "value": 0},
                blocking=False,
            )
            actions.append("input_limit=0")
        if current_output > 0:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": output_limit_entity, "value": 0},
                blocking=False,
            )
            actions.append("output_limit=0")
        self.last_actuation = {
            "status": "disabled_zeroed" if actions else "disabled_already_zero",
            "reason": "strategy_disabled",
            "actions": actions,
            "current_input_limit_w": current_input,
            "current_output_limit_w": current_output,
        }

    async def _async_apply_command(self, command: StrategyCommand, options: StrategyOptions) -> None:
        """Apply the command to Zendure with anti-oscillation guardrails."""
        ac_mode_entity = self._entity_id(CONF_ZENDURE_AC_MODE_ENTITY, "select.hoa1nan7n331666_acmode")
        input_limit_entity = self._entity_id(CONF_ZENDURE_INPUT_LIMIT_ENTITY, "number.hoa1nan7n331666_inputlimit")
        output_limit_entity = self._entity_id(CONF_ZENDURE_OUTPUT_LIMIT_ENTITY, "number.hoa1nan7n331666_outputlimit")
        required_entities = [ac_mode_entity, input_limit_entity, output_limit_entity]
        if not all(self._state_available(entity) for entity in required_entities):
            self.last_actuation = {"status": "skipped", "reason": "control_entity_unavailable"}
            return
        if not self._grid_inputs_fresh():
            self.last_actuation = {"status": "skipped", "reason": "grid_inputs_stale"}
            return

        targets = zendure_targets(command)
        current_mode = self.hass.states.get(ac_mode_entity).state
        current_input = self._raw_state_float(input_limit_entity)
        current_output = self._raw_state_float(output_limit_entity)
        actions: list[str] = []

        if command.mode != COMMAND_IDLE and should_write_mode(
            current_mode,
            targets.mode_option,
            self._state_age_s(ac_mode_entity),
        ):
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": ac_mode_entity, "option": targets.mode_option},
                blocking=False,
            )
            actions.append(f"mode={targets.mode_option}")

        if should_write_limit(
            current_input,
            targets.input_limit_w,
            self._state_age_s(input_limit_entity),
            options,
            force_zero=True,
        ):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": input_limit_entity, "value": targets.input_limit_w},
                blocking=False,
            )
            actions.append(f"input_limit={targets.input_limit_w}")

        if should_write_limit(
            current_output,
            targets.output_limit_w,
            self._state_age_s(output_limit_entity),
            options,
            force_zero=True,
        ):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": output_limit_entity, "value": targets.output_limit_w},
                blocking=False,
            )
            actions.append(f"output_limit={targets.output_limit_w}")

        self.last_actuation = {
            "status": "written" if actions else "no_change",
            "actions": actions,
            "target_mode": targets.mode_option,
            "target_input_limit_w": targets.input_limit_w,
            "target_output_limit_w": targets.output_limit_w,
            "current_mode": current_mode,
            "current_input_limit_w": current_input,
            "current_output_limit_w": current_output,
        }

    def _record_parallel_sample(self, inputs: StrategyInputs, command: StrategyCommand) -> None:
        reference_mode_entity = self.entry.data.get(CONF_REFERENCE_MODE_ENTITY)
        reference_power_entity = self.entry.data.get(CONF_REFERENCE_POWER_ENTITY)
        if reference_mode_entity and reference_power_entity:
            reference_mode = self._state_value(CONF_REFERENCE_MODE_ENTITY)
            reference_power = self._state_float(CONF_REFERENCE_POWER_ENTITY)
            if reference_mode not in ("unknown", "unavailable", "none", ""):
                self._new_commands.append(command)
                self._reference_modes.append(reference_mode)
                self._reference_powers_w.append(reference_power)

        reference_data = self._reference_data_points()
        if reference_data is not None:
            self._new_data_points.append(
                {
                    "house_load_no_ev_w": command.house_load_no_ev_w,
                    "house_load_total_w": command.house_load_total_w,
                    "pv_w": inputs.pv_w,
                    "residual_no_ev_w": command.residual_no_ev_w,
                    "residual_with_ev_w": command.residual_with_ev_w,
                }
            )
            self._reference_input_points.append(reference_data)

        self._new_commands = self._new_commands[-288:]
        self._reference_modes = self._reference_modes[-288:]
        self._reference_powers_w = self._reference_powers_w[-288:]
        self._new_data_points = self._new_data_points[-288:]
        self._reference_input_points = self._reference_input_points[-288:]
        self.parallel_evaluation = evaluate_parallel_commands(
            self._new_commands,
            self._reference_modes,
            self._reference_powers_w,
            self._new_data_points,
            self._reference_input_points,
        )

    def _disabled_display_command(self, command: StrategyCommand) -> StrategyCommand:
        """Return the safe UI command while actuation is disabled."""
        return replace(
            command,
            mode=COMMAND_IDLE,
            power_w=0,
            reason="strategy_disabled_external_control",
            allowed_discharge_load_w=0,
        )

    def _reference_data_points(self) -> dict[str, float] | None:
        """Return comparable comparison live data points if they are available."""
        entities = {
            "house_load_no_ev_w": "sensor.battery_strategy_house_load_actual_power_now",
            "house_load_total_w": "sensor.battery_strategy_house_load_total_actual_power_now",
            "pv_w": "sensor.battery_strategy_pv_generation_actual_power_now",
            "residual_no_ev_w": "sensor.battery_strategy_residual_net_no_battery_no_ev",
            "residual_with_ev_w": "sensor.battery_strategy_residual_net_no_battery_with_ev",
        }
        if not all(self._state_available(entity_id) for entity_id in entities.values()):
            return None
        return {key: self._raw_state_float(entity_id) for key, entity_id in entities.items()}

    def _write_parallel_state(self, data: dict) -> None:
        """Write the latest read-only parallel result for server-side inspection."""
        path = Path(self.hass.config.path("battery_strategy_parallel_state.json"))
        payload = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "strategy_enabled": data["strategy_enabled"],
            "send_commands": data["send_commands"],
            "optimizer_age_s": data.get("optimizer_age_s"),
            "plan_to_live": asdict(data["plan_to_live"]),
            "inputs": asdict(data["inputs"]),
            "options": asdict(data["options"]),
            "command": asdict(data["command"]),
            "calculated_command": asdict(data["calculated_command"]),
            "plan": asdict(data["plan"]),
            "plan_comparison": asdict(data["plan_comparison"]),
            "parallel": asdict(data["parallel"]),
            "parallel_passed": data["parallel"].passed,
            "parallel_input_passed": data["parallel"].input_passed,
            "parallel_command_passed": data["parallel"].command_passed,
            "actuation": data["actuation"],
        }
        write_json_atomic(path, payload)

    def _append_command_trace(self, data: dict) -> None:
        """Append a compact command trace for later 12h/48h analysis."""
        path = Path(self.hass.config.path("battery_strategy_hacs_command_trace.json"))
        now = dt.datetime.now(dt.timezone.utc)
        reference_mode = self._state_value(CONF_REFERENCE_MODE_ENTITY)
        reference_power = self._state_float(CONF_REFERENCE_POWER_ENTITY)
        command = data["command"]
        calculated_command = data["calculated_command"]
        plan = data["plan"]
        plan_comparison = data["plan_comparison"]
        inputs = data["inputs"]
        directive = data["plan_to_live"]
        trace = []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                trace = raw
        except (OSError, json.JSONDecodeError):
            trace = []
        cutoff = (now - dt.timedelta(days=7)).timestamp()
        trace = [item for item in trace if float(item.get("ts", 0.0)) >= cutoff]
        trace.append(
            {
                "ts": now.timestamp(),
                "iso": now.isoformat(),
                "mode": command.mode,
                "power_w": command.power_w,
                "reason": command.reason,
                "calculated_mode": calculated_command.mode,
                "calculated_power_w": calculated_command.power_w,
                "calculated_reason": calculated_command.reason,
                "reference_mode": reference_mode,
                "reference_power_w": reference_power,
                "send_commands": data["send_commands"],
                "strategy_enabled": data["strategy_enabled"],
                "plan_input_passed": plan_comparison.plan_input_passed,
                "tomorrow_strategy_passed": plan_comparison.tomorrow_strategy_passed,
                "forty8h_strategy_passed": plan_comparison.forty8h_strategy_passed,
                "live_command_passed": plan_comparison.live_command_passed,
                "override_active": plan_comparison.override_active,
                "grid_import_w": round(inputs.grid_import_w),
                "grid_export_w": round(inputs.grid_export_w),
                "pv_w": round(inputs.pv_w),
                "battery_power_w": round(inputs.battery_power_w),
                "ev_power_w": round(inputs.ev_power_w),
                "soc_pct": round(inputs.soc_pct, 1),
                "current_plan_points": len(plan.points),
                "optimizer_age_s": data.get("optimizer_age_s"),
                "plan_mode": plan.current_mode,
                "plan_power_w": plan.current_power_w,
                "plan_to_live": asdict(directive),
            }
        )
        write_json_atomic(path, trace[-60480:])

    def _publish_parallel_dashboard_states(self, data: dict) -> None:
        """Publish simple read-only state-machine values for the parallel dashboard."""
        inputs = data["inputs"]
        command = data["command"]
        calculated_command = data["calculated_command"]
        plan = data["plan"]
        plan_comparison = data["plan_comparison"]
        parallel = data["parallel"]
        directive = data["plan_to_live"]
        generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        state_attrs = {
            "generated_at": generated_at,
            "strategy_enabled": data["strategy_enabled"],
            "send_commands": data["send_commands"],
            "grid_import_w": round(inputs.grid_import_w),
            "grid_export_w": round(inputs.grid_export_w),
            "pv_w": round(inputs.pv_w),
            "battery_power_w": round(inputs.battery_power_w),
            "ev_power_w": round(inputs.ev_power_w),
            "soc_pct": round(inputs.soc_pct, 1),
            "mode": command.mode,
            "power_w": command.power_w,
            "reason": command.reason,
            "calculated_mode": calculated_command.mode,
            "calculated_power_w": calculated_command.power_w,
            "calculated_reason": calculated_command.reason,
            "planned_mode": plan.current_mode,
            "planned_power_w": plan.current_power_w,
            "plan_to_live": asdict(directive),
            "optimizer_age_s": data.get("optimizer_age_s"),
            "plan_points": len(plan.points),
            "plan_input_passed": plan_comparison.plan_input_passed,
            "tomorrow_strategy_passed": plan_comparison.tomorrow_strategy_passed,
            "forty8h_strategy_passed": plan_comparison.forty8h_strategy_passed,
            "live_command_passed": plan_comparison.live_command_passed,
            "override_active": plan_comparison.override_active,
            "parallel_samples": parallel.samples,
            "parallel_input_samples": parallel.input_samples,
            "parallel_matching_mode_samples": parallel.matching_mode_samples,
            "parallel_max_power_delta_w": parallel.max_power_delta_w,
        }
        self.hass.states.async_set("sensor.battery_strategy_parallel_dashboard_state", command.mode, state_attrs)
        self.hass.states.async_set("sensor.battery_strategy_parallel_dashboard_generated_at", generated_at)
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_send_commands",
            str(data["send_commands"]).lower(),
        )
        self.hass.states.async_set("sensor.battery_strategy_parallel_dashboard_mode", command.mode)
        self.hass.states.async_set("sensor.battery_strategy_parallel_dashboard_reason", command.reason)
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_slot_start",
            _format_ts_ms(directive.slot_start_ts),
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_slot_end",
            _format_ts_ms(directive.slot_end_ts),
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_pv_charge_allowed",
            str(directive.pv_charge_allowed).lower(),
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_must_charge",
            round(directive.must_charge_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_must_charge_remaining",
            round(directive.must_charge_remaining_kwh, 3),
            {"unit_of_measurement": "kWh"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_grid_charge_allowed",
            str(directive.grid_charge_allowed).lower(),
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_discharge_budget",
            round(directive.discharge_budget_kwh, 3),
            {"unit_of_measurement": "kWh"},
        )
        optimizer_budget = 0.0
        if plan.points:
            optimizer_budget = round(max(0.0, float(getattr(plan.points[0], "discharge_budget_kwh", 0.0))), 3)
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_optimizer_discharge_budget",
            optimizer_budget,
            {"unit_of_measurement": "kWh"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_command_power",
            round(command.power_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set("sensor.battery_strategy_parallel_dashboard_samples", parallel.samples)
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_matching_samples",
            parallel.matching_mode_samples,
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_max_power_delta",
            round(parallel.max_power_delta_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_grid_import",
            round(inputs.grid_import_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_grid_export",
            round(inputs.grid_export_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_ev_power",
            round(inputs.ev_power_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_battery_power",
            round(inputs.battery_power_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_residual_no_ev",
            round(command.residual_no_ev_w),
            {"unit_of_measurement": "W"},
        )
        self.hass.states.async_set(
            "sensor.battery_strategy_parallel_dashboard_residual_with_ev",
            round(command.residual_with_ev_w),
            {"unit_of_measurement": "W"},
        )

    def _price_points_sync(
        self,
        now: dt.datetime,
        options: StrategyOptions,
        reference_output: dict | None = None,
    ) -> list[PricePoint]:
        """Return price points from Tibber Prices storage or comparison profiles."""
        prices = []
        try:
            prices = read_tibber_price_points(
                self.hass.config.path(".storage/tibber_prices.interval_pool.*"),
                now,
                options.planning_horizon_h,
            )
        except (OSError, ValueError):
            prices = []
        if prices:
            return prices

        reference_output = reference_output or self._reference_plan_attrs_sync()
        today = price_points_from_profile(reference_output.get("profile_today_price"))
        tomorrow = price_points_from_profile(reference_output.get("profile_tomorrow_price"))
        return sorted(today + tomorrow, key=lambda item: item.ts_ms)

    def _compare_optimizer_plan(
        self,
        plan: StrategyPlan,
        command: StrategyCommand,
        reference_output: dict | None = None,
    ) -> PlanComparison:
        """Compare internal optimizer output with the reference attributes."""
        reference_output = reference_output or self._reference_plan_attrs_sync()
        reference_mode = self._state_value(CONF_REFERENCE_MODE_ENTITY)
        reference_power = self._state_float(CONF_REFERENCE_POWER_ENTITY)
        if command.reason.startswith("manual_"):
            reference_mode = command.mode
            reference_power = command.power_w
        return compare_optimizer_plan(plan, reference_output, command.mode, command.power_w, reference_mode, reference_power)

    def _reference_plan_attrs_sync(self) -> dict:
        """Return reference attributes when available."""
        entity_id = self.entry.data.get(CONF_REFERENCE_OUTPUT_ENTITY, "")
        if not entity_id:
            return {}
        state = self.hass.states.get(entity_id)
        return dict(state.attributes) if state is not None else {}


def _format_ts_ms(ts_ms: int) -> str:
    """Return a local readable timestamp for dashboard sensors."""
    if not ts_ms:
        return ""
    return dt.datetime.fromtimestamp(ts_ms / 1000.0, LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

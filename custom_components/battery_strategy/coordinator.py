"""Data coordinator for Battery Strategy."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import asdict, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .actuator import HomeAssistantZendureActuator
from .const import (
    BATTERY_PROFILE_ZENDURE,
    COMMAND_IDLE,
    COMMAND_OUTPUT,
    CONF_BATTERY_CAPACITY_KWH,
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
    CONF_PRICE_ENTITY,
    CONF_PV_CAPACITY_KWP,
    CONF_PV_INVERTER_POWER_KW,
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
from .contracts import (
    ForecastRequest,
    PlanCompilationState,
    QualityFlag,
    SlotKey,
    SlotProgress,
)
from .contracts.common import SLOT_MS
from .feature_store import (
    CompressedFeatureStore,
    ExecutorFeatureStore,
    FeatureAggregator,
    FeatureObservation,
)
from .live_control import DirectionHysteresis, P1UpdateGate
from .load_components import (
    LoadComponentCollection,
    add_central_weather,
    collect_load_components,
)
from .models import StrategyCommand, StrategyInputs, StrategyOptions
from .optimizer_adapter import OptimizerEngineAdapter
from .optimizer_state import last_known_soc_pct
from .plan_compiler import DeterministicPlanCompiler
from .plan_compiler_adapter import (
    closed_published_directive,
    contract_plan_from_strategy_plan,
    published_directive_from_contract,
)
from .plan_models import PlanLiveDirective
from .planner import BackgroundPlanner
from .strategy import calculate_command, live_command_from_directive
from .weather import OpenMeteoWeatherProvider

LOGGER = logging.getLogger(__name__)
OPTIMIZER_PREFETCH_LEAD_S = 60
SOC_BRIDGE_MAX_AGE_S = 300
SOC_COLD_START_PLACEHOLDER_PCT = 50.0
EV_POWER_BRIDGE_MAX_AGE_S = 180
OPTIMIZER_STATE_FILE = "battery_strategy_optimizer_state.json"
FEATURE_STORE_FILE = "battery_strategy_features.json.gz"
COMMAND_TRACE_FILE = "battery_strategy_command_trace.jsonl"
COMMAND_TRACE_MAX_BYTES = 64 * 1024 * 1024
COMMAND_TRACE_RETAIN_LINES = 50000
GRID_INPUT_MAX_AGE_S = 30


def _load_last_known_soc_pct(path: Path) -> float | None:
    """Load the most recent valid real battery SoC from optimizer state."""
    return last_known_soc_pct(path)


class BatteryStrategyCoordinator(DataUpdateCoordinator):
    """Collect HA states, calculate the strategy, and apply commands."""

    def __init__(
        self,
        hass,
        entry,
        update_interval,
        last_known_soc_pct: float | None = None,
        last_optimizer_output: dict | None = None,
        feature_store: ExecutorFeatureStore | None = None,
        feature_history=(),
    ):
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
        self._feature_history = tuple(feature_history)
        self._optimizer_engine = OptimizerEngineAdapter(hass, entry)
        self._optimizer_engine.hydrate_output(last_optimizer_output)
        self._planner = BackgroundPlanner(hass, self._optimizer_engine)
        self._optimizer_attrs: dict = {}
        self.last_actuation: dict[str, object] = {"status": "not_started"}
        self._active_directive_slot_id: str | None = None
        self._active_directive_slot_end_ts_ms: int = 0
        self._last_optimizer_force_key: str | None = None
        self._slot_charged_kwh = 0.0
        self._slot_discharged_kwh = 0.0
        self._plan_compiler = DeterministicPlanCompiler()
        self._plan_compilation_state = PlanCompilationState()
        self._plan_compiler_error: str | None = None
        self._last_live_accounting_ts: dt.datetime | None = None
        self._last_actual_battery_power_w: float | None = None
        self._last_known_soc_pct = last_known_soc_pct
        self._soc_control_ready = last_known_soc_pct is not None
        self._soc_recovered = False
        self._last_valid_soc_at = (
            dt.datetime.now(dt.timezone.utc) if last_known_soc_pct is not None else None
        )
        self._actuator = HomeAssistantZendureActuator(
            hass,
            str(entry.data.get(CONF_ZENDURE_AC_MODE_ENTITY, "")),
            str(entry.data.get(CONF_ZENDURE_INPUT_LIMIT_ENTITY, "")),
            str(entry.data.get(CONF_ZENDURE_OUTPUT_LIMIT_ENTITY, "")),
        )
        self._p1_update_gate = P1UpdateGate()
        self._direction_hysteresis = DirectionHysteresis()
        self._live_event_unsubs: list[object] = []
        self._last_known_ev_power_w = 0.0
        self._last_valid_ev_at: dt.datetime | None = None
        self._ev_control_ready = not bool(entry.data.get(CONF_EV_POWER_ENTITY))
        self._strategy_was_enabled = bool(entry.options.get("strategy_enabled", False))
        self._disabled_zeroed = not self._strategy_was_enabled
        self._feature_store = feature_store or ExecutorFeatureStore(
            CompressedFeatureStore(Path(hass.config.path(FEATURE_STORE_FILE))),
            hass.async_add_executor_job,
        )
        self._feature_aggregator = FeatureAggregator()
        self._load_components = LoadComponentCollection()
        self._weather = ()
        self._weather_error: str | None = None
        self._weather_refresh_key: tuple[dt.date, int] | None = None
        self._weather_task: asyncio.Task | None = None
        self._weather_provider = OpenMeteoWeatherProvider(
            async_get_clientsession(hass), hass.config.latitude, hass.config.longitude
        )

    def set_manual_override(
        self, mode: str, power_w: float, duration_min: int = 0
    ) -> None:
        """Set an in-memory manual override."""
        self._manual_mode = mode
        self._manual_power_w = max(0.0, float(power_w))
        if duration_min > 0:
            self._manual_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                minutes=duration_min
            )
        else:
            self._manual_until = None

    def clear_manual_override(self) -> None:
        """Clear manual override."""
        self._manual_mode = MANUAL_OFF
        self._manual_power_w = 0.0
        self._manual_until = None

    def async_start_live_tracking(self) -> None:
        """Track meter and safety inputs for event-driven live control."""
        if self._live_event_unsubs:
            return
        grid_entities = self._grid_entity_ids()
        if grid_entities:
            self._live_event_unsubs.append(
                async_track_state_change_event(
                    self.hass, grid_entities, self._async_grid_state_changed
                )
            )
        critical_entities = [
            entity
            for entity in (
                self.entry.data.get(CONF_EV_POWER_ENTITY),
                self.entry.data.get(CONF_BATTERY_SOC_ENTITY),
            )
            if entity
        ]
        if critical_entities:
            self._live_event_unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    critical_entities,
                    self._async_critical_state_changed,
                )
            )

    def _grid_entity_ids(self) -> list[str]:
        """Return only the authoritative grid entities for the configured mode."""
        mode = self.entry.data.get(CONF_GRID_MODE, GRID_MODE_THREE_PHASE)
        if mode == GRID_MODE_THREE_PHASE:
            keys = (CONF_GRID_L1_ENTITY, CONF_GRID_L2_ENTITY, CONF_GRID_L3_ENTITY)
        elif mode == GRID_MODE_IMPORT_EXPORT:
            keys = (CONF_GRID_IMPORT_ENTITY, CONF_GRID_EXPORT_ENTITY)
        elif mode == GRID_MODE_SIGNED:
            keys = (CONF_SIGNED_GRID_POWER_ENTITY,)
        else:
            keys = ()
        return [str(self.entry.data[key]) for key in keys if self.entry.data.get(key)]

    async def _async_grid_state_changed(self, event) -> None:
        """Run the live layer on P1 changes using Zendure's fast/normal gate."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (
            "unknown",
            "unavailable",
            "none",
            "",
        ):
            await self.async_request_refresh()
            return
        grid_import_w, grid_export_w = self._grid_import_export()
        signed_p1_w = grid_import_w - grid_export_w
        if self._p1_update_gate.should_refresh(signed_p1_w, time.monotonic()):
            await self.async_request_refresh()

    async def _async_critical_state_changed(self, _event) -> None:
        """Apply EV and SoC policy changes without waiting for the P1 gate."""
        await self.async_request_refresh()

    def _account_actual_battery_power(self, now: dt.datetime) -> None:
        """Account measured battery energy inside the active slot."""
        if (
            self._last_live_accounting_ts is None
            or self._last_actual_battery_power_w is None
        ):
            return
        elapsed_h = (
            max(0.0, (now - self._last_live_accounting_ts).total_seconds()) / 3600.0
        )
        if elapsed_h <= 0.0 or elapsed_h > 0.25:
            return
        energy_kwh = float(self._last_actual_battery_power_w) * elapsed_h / 1000.0
        if energy_kwh < 0.0:
            self._slot_charged_kwh += abs(energy_kwh)
        elif energy_kwh > 0.0:
            self._slot_discharged_kwh += energy_kwh

    def _sync_slot_progress(self, slot_start_ms: int) -> None:
        """Reset measured progress exactly once at a 15-minute boundary."""
        slot_id = str(max(0, int(slot_start_ms)))
        if slot_id == self._active_directive_slot_id:
            return
        self._active_directive_slot_id = slot_id
        self._active_directive_slot_end_ts_ms = int(slot_start_ms) + SLOT_MS
        self._slot_charged_kwh = 0.0
        self._slot_discharged_kwh = 0.0

    def _compile_authoritative_directive(
        self,
        plan,
        options: StrategyOptions,
        inputs: StrategyInputs,
        now_ms: int,
    ) -> PlanLiveDirective:
        """Compile the directive consumed by the established live controller."""
        if not plan.points:
            self._plan_compilation_state = PlanCompilationState()
            self._plan_compiler_error = "no_plan"
            return closed_published_directive(options)
        try:
            contract_plan = contract_plan_from_strategy_plan(plan, options, now_ms)
            current_slot = contract_plan.slots[0].slot
            compiled, next_state = self._plan_compiler.compile(
                contract_plan,
                SlotProgress(
                    slot=current_slot,
                    charged_kwh=max(0.0, self._slot_charged_kwh),
                    discharged_kwh=max(0.0, self._slot_discharged_kwh),
                    soc_pct=float(inputs.soc_pct),
                ),
                self._plan_compilation_state,
                issued_at_ms=now_ms,
            )
            self._plan_compilation_state = next_state
            self._plan_compiler_error = None
            return published_directive_from_contract(compiled, plan, options)
        except Exception as err:  # noqa: BLE001 - control must fail closed.
            self._plan_compilation_state = PlanCompilationState()
            self._plan_compiler_error = f"{type(err).__name__}: {err}"
            LOGGER.error("Plan compiler failed closed: %s", self._plan_compiler_error)
            return closed_published_directive(
                options,
                slot_start_ms=int(plan.points[0].ts_ms),
            )

    async def _async_update_data(self):
        """Fetch current states and calculate command."""
        if (
            self._manual_until is not None
            and dt.datetime.now(dt.timezone.utc) >= self._manual_until
        ):
            self.clear_manual_override()

        options = self._strategy_options()
        inputs = self._strategy_inputs()
        now = dt.datetime.now(dt.timezone.utc)
        local_now = now.astimezone(ZoneInfo(self.hass.config.time_zone))
        self._load_components = collect_load_components(
            self.hass, self.entry, local_now
        )
        current_slot_ms = int(now.timestamp() * 1000) // SLOT_MS * SLOT_MS
        current_weather = next(
            (item for item in self._weather if item.slot.start_ms == current_slot_ms),
            None,
        )
        self._load_components = add_central_weather(
            self._load_components, current_weather
        )
        self._optimizer_engine.set_forecast_environment(
            self._feature_history,
            self._weather,
            self._load_components.drivers,
            self._load_components.specs,
        )
        self._schedule_weather_refresh(now, local_now)
        try:
            finalized_features = self._feature_aggregator.observe(
                FeatureObservation(
                    timestamp_ms=int(now.timestamp() * 1000),
                    grid_import_w=inputs.grid_import_w,
                    grid_export_w=inputs.grid_export_w,
                    pv_generation_w=inputs.pv_w,
                    battery_power_w=inputs.battery_power_w,
                    ev_charge_w=inputs.ev_power_w,
                    price_ct_per_kwh=self._current_price_ct(now),
                    quality_flags=self._feature_quality_flags(),
                    load_components_w=self._load_components.powers_w,
                    load_component_features=self._load_components.features,
                )
            )
        except Exception as err:  # noqa: BLE001 - collection must not stop control.
            finalized_features = ()
            self._feature_store.last_error = f"{type(err).__name__}: {err}"
            LOGGER.warning("Feature-store aggregation failed: %s", err)
        self._account_actual_battery_power(now)
        force_optimizer = self._should_force_optimizer(now) or self._soc_recovered
        self._soc_recovered = False
        simple_command = calculate_command(inputs, options)
        optimizer_scheduled = False
        if self._soc_control_ready:
            runtime_context = self._optimizer_engine.runtime_context(inputs, options)
            optimizer_scheduled = self._planner.maybe_schedule(
                inputs, options, runtime_context, force=force_optimizer
            )
        plan, self._optimizer_attrs = self._planner.current(inputs, options)
        now_ms = int(now.timestamp() * 1000)
        slot_start_ms = int(plan.points[0].ts_ms) if plan.points else 0
        self._sync_slot_progress(slot_start_ms)
        directive = self._compile_authoritative_directive(
            plan,
            options,
            inputs,
            now_ms,
        )
        command = live_command_from_directive(
            directive, simple_command, inputs, options
        )
        strategy_enabled = bool(self.entry.options.get("strategy_enabled", False))
        if strategy_enabled:
            command = self._direction_hysteresis.apply(
                command,
                inputs.battery_power_w,
                time.monotonic(),
            )
        calculated_command = command
        display_command = (
            command if strategy_enabled else self._disabled_display_command(command)
        )
        self._last_actual_battery_power_w = inputs.battery_power_w
        self._last_live_accounting_ts = now
        data = {
            "inputs": inputs,
            "options": options,
            "command": display_command,
            "calculated_command": calculated_command,
            "plan": plan,
            "optimizer_attrs": self._optimizer_attrs,
            "plan_to_live": directive,
            "plan_compiler_error": self._plan_compiler_error,
            "send_commands": strategy_enabled,
            "strategy_enabled": strategy_enabled,
            "actuation": self.last_actuation,
            "optimizer_age_s": self._optimizer_engine.age_s(),
            "optimizer_forced": force_optimizer,
            "optimizer_scheduled": optimizer_scheduled,
            "optimizer_running": self._planner.running,
            "optimizer_error": self._planner.last_error,
            "soc_control_ready": self._soc_control_ready,
            "soc_estimate_stale": not self._soc_control_ready,
        }
        if strategy_enabled:
            self._strategy_was_enabled = True
            self._disabled_zeroed = False
            await self._async_apply_command(calculated_command, options)
            data["actuation"] = self.last_actuation
        elif not self._disabled_zeroed:
            self._disabled_zeroed = await self._async_zero_limits_once()
            self._strategy_was_enabled = False
            data["actuation"] = self.last_actuation
        else:
            self._strategy_was_enabled = False
            self.last_actuation = {
                "status": "disabled_no_write",
                "reason": "strategy_disabled",
            }
            data["actuation"] = self.last_actuation
        if bool(self.entry.options.get("trace_enabled", False)):
            await self.hass.async_add_executor_job(self._append_command_trace, data)
        if finalized_features:
            try:
                await self._feature_store.upsert(finalized_features)
                self._feature_history = await self._feature_store.load(
                    0, int(now.timestamp() * 1000) + 1
                )
                self._optimizer_engine.set_forecast_environment(
                    self._feature_history,
                    self._weather,
                    self._load_components.drivers,
                    self._load_components.specs,
                )
            except (OSError, ValueError, TypeError) as err:
                self._feature_store.last_error = f"{type(err).__name__}: {err}"
                LOGGER.warning("Feature-store write failed: %s", err)
        data["feature_store"] = self._feature_store.diagnostics(
            self._feature_aggregator.active_coverage
        )
        data["forecast_environment"] = {
            "component_profiles": [
                {
                    "component_key": item.component_key,
                    "profile": item.profile,
                }
                for item in self._load_components.specs
            ],
            "valid_component_meter_count": len(self._load_components.powers_w),
            "weather_slot_count": len(self._weather),
            "weather_error": self._weather_error,
        }
        return data

    def _schedule_weather_refresh(
        self, now: dt.datetime, local_now: dt.datetime
    ) -> None:
        """Refresh weather in the background at most once per quarter-hour."""
        key = (local_now.date(), local_now.hour * 4 + local_now.minute // 15)
        if key == self._weather_refresh_key:
            return
        if self._weather_task is not None and not self._weather_task.done():
            return
        self._weather_refresh_key = key
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(local_start.astimezone(dt.timezone.utc).timestamp() * 1000)
        start_ms = start_ms // SLOT_MS * SLOT_MS
        slots = tuple(
            SlotKey(start_ms + index * SLOT_MS, start_ms + (index + 1) * SLOT_MS)
            for index in range(4 * 96)
        )
        request = ForecastRequest(
            int(now.timestamp() * 1000), self.hass.config.time_zone, slots
        )
        self._weather_task = self.hass.async_create_task(
            self._async_refresh_weather(request),
            name="battery_strategy_weather",
        )

    async def _async_refresh_weather(self, request: ForecastRequest) -> None:
        """Update the normalized weather snapshot without affecting control."""
        try:
            self._weather = await self._weather_provider.load(request)
            self._weather_error = self._weather_provider.last_error
            self._optimizer_engine.set_forecast_environment(
                self._feature_history,
                self._weather,
                self._load_components.drivers,
                self._load_components.specs,
            )
            if self._weather_error:
                LOGGER.warning(
                    "Weather refresh failed; using bounded estimated cache: %s",
                    self._weather_error,
                )
        except Exception as err:  # noqa: BLE001 - weather is optional input.
            self._weather = ()
            self._weather_error = f"{type(err).__name__}: {err}"
            self._optimizer_engine.set_forecast_environment(
                self._feature_history,
                (),
                self._load_components.drivers,
                self._load_components.specs,
            )
            LOGGER.warning("Shadow weather refresh failed: %s", err)

    def _current_price_ct(self, now: dt.datetime) -> float | None:
        """Return the configured Tibber Prices value normalized to ct/kWh."""
        entity_id = self.entry.data.get(CONF_PRICE_ENTITY)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unknown", "unavailable", "none", ""):
            return None
        intervals = state.attributes.get("data") or []
        selected: tuple[dt.datetime, float] | None = None
        for item in intervals:
            if not isinstance(item, dict):
                continue
            start_raw = (
                item.get("start_time") or item.get("startsAt") or item.get("start")
            )
            price_raw = item.get("price_per_kwh", item.get("price", item.get("total")))
            if start_raw is None or price_raw is None:
                continue
            try:
                start = dt.datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                price_value = float(price_raw)
                price_ct = price_value if price_value >= 2.0 else price_value * 100.0
            except (TypeError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=dt.timezone.utc)
            if start <= now and (selected is None or start > selected[0]):
                selected = (start, price_ct)
        if selected is not None:
            return selected[1]
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = str(state.attributes.get("unit_of_measurement") or "").lower()
        return value if "ct" in unit else value * 100.0

    def _feature_quality_flags(self) -> tuple[QualityFlag, ...]:
        """Describe missing semantic inputs without changing live behavior."""
        flags: list[QualityFlag] = []
        if not self._grid_inputs_fresh():
            flags.append(QualityFlag.MISSING_GRID)
        pv_entity = self.entry.data.get(CONF_PV_POWER_ENTITY)
        if not pv_entity or not self._state_available(pv_entity):
            flags.append(QualityFlag.MISSING_PV)
        if not self._battery_measurement_available():
            flags.append(QualityFlag.MISSING_BATTERY)
        if self.entry.data.get(CONF_EV_POWER_ENTITY) and not self._ev_control_ready:
            flags.append(QualityFlag.MISSING_EV)
        return tuple(flags)

    def _battery_measurement_available(self) -> bool:
        """Return whether the battery power reconstruction has usable inputs."""
        if self.entry.data.get(CONF_BATTERY_PROFILE) != BATTERY_PROFILE_ZENDURE:
            entity = self.entry.data.get(CONF_BATTERY_POWER_ENTITY)
            return bool(entity and self._state_available(entity))
        entities = (
            self.entry.data.get(CONF_ZENDURE_AC_MODE_ENTITY),
            self.entry.data.get(CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY),
            self.entry.data.get(CONF_ZENDURE_PACK_INPUT_POWER_ENTITY),
            self.entry.data.get(CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY),
            self.entry.data.get(CONF_ZENDURE_GRID_INPUT_POWER_ENTITY),
        )
        return all(entity and self._state_available(entity) for entity in entities)

    def _should_force_optimizer(self, now: dt.datetime) -> bool:
        """Return whether the optimizer should refresh around the current slot boundary."""
        if self._active_directive_slot_end_ts_ms <= 0:
            return False
        now_ms = int(now.timestamp() * 1000)
        lead_ms = OPTIMIZER_PREFETCH_LEAD_S * 1000
        if now_ms < self._active_directive_slot_end_ts_ms - lead_ms:
            return False
        phase = (
            "expired" if now_ms >= self._active_directive_slot_end_ts_ms else "prefetch"
        )
        key = f"{self._active_directive_slot_id}:{self._active_directive_slot_end_ts_ms}:{phase}"
        if key == self._last_optimizer_force_key:
            return False
        self._last_optimizer_force_key = key
        return True

    def _strategy_options(self) -> StrategyOptions:
        opts = dict(self.entry.options)
        manual_mode = (
            self._manual_mode
            if self._manual_mode != MANUAL_OFF
            else opts.get("manual_mode", MANUAL_OFF)
        )
        manual_power = (
            self._manual_power_w
            if self._manual_mode != MANUAL_OFF
            else float(opts.get("manual_power_w", 0.0))
        )
        return StrategyOptions(
            pv_charging=opts.get("pv_charging", PV_CHARGING_ON),
            grid_charging=opts.get("grid_charging", GRID_CHARGING_OFF),
            discharge=opts.get("discharge", DISCHARGE_LOAD),
            pv_to_ev_first=bool(opts.get("pv_to_ev_first", True)),
            discharge_during_ev_charging=bool(
                opts.get("discharge_during_ev_charging", True)
            ),
            battery_may_feed_ev=bool(opts.get("battery_may_feed_ev", False)),
            ev_active_threshold_w=float(opts.get("ev_active_threshold_w", 300.0)),
            min_soc_pct=float(opts.get("min_soc_pct", 10.0)),
            max_soc_pct=float(opts.get("max_soc_pct", 100.0)),
            max_charge_power_w=float(opts.get("max_charge_power_w", 2400.0)),
            max_discharge_power_w=float(opts.get("max_discharge_power_w", 2400.0)),
            min_command_power_w=float(opts.get("min_command_power_w", 20.0)),
            min_command_delta_w=float(opts.get("min_command_delta_w", 5.0)),
            round_trip_efficiency=float(opts.get("round_trip_efficiency", 0.80)),
            min_margin_ct_per_kwh=float(opts.get("min_margin_ct_per_kwh", 2.0)),
            planning_horizon_h=int(opts.get("planning_horizon_h", 48)),
            feed_in_tariff_ct_per_kwh=float(opts.get("feed_in_tariff_ct_per_kwh", 0.0)),
            battery_capacity_kwh=float(opts.get(CONF_BATTERY_CAPACITY_KWH, 6.0)),
            pv_capacity_kwp=float(opts.get(CONF_PV_CAPACITY_KWP, 0.0)),
            pv_inverter_power_kw=float(opts.get(CONF_PV_INVERTER_POWER_KW, 0.0)),
            manual_mode=manual_mode,
            manual_power_w=manual_power,
        )

    def _strategy_inputs(self) -> StrategyInputs:
        grid_import, grid_export = self._grid_import_export()
        return StrategyInputs(
            grid_import_w=grid_import,
            grid_export_w=grid_export,
            pv_w=self._state_power_w(CONF_PV_POWER_ENTITY),
            battery_power_w=self._battery_power_w(),
            ev_power_w=self._ev_power_w(),
            soc_pct=self._battery_soc_pct(),
        )

    def _battery_soc_pct(self) -> float:
        """Return the last real SoC estimate and gate control when it is stale."""
        entity_id = self.entry.data.get(CONF_BATTERY_SOC_ENTITY)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in (
                "unknown",
                "unavailable",
                "none",
                "",
            ):
                try:
                    value = float(state.state)
                except (TypeError, ValueError):
                    value = None
                if value is not None and 0.0 <= value <= 100.0:
                    was_control_ready = self._soc_control_ready
                    self._last_known_soc_pct = value
                    self._soc_control_ready = True
                    self._soc_recovered = not was_control_ready
                    self._last_valid_soc_at = dt.datetime.now(dt.timezone.utc)
                    return value
        last_valid_soc_at = getattr(
            self, "_last_valid_soc_at", dt.datetime.now(dt.timezone.utc)
        )
        if self._last_known_soc_pct is not None:
            age_s = (
                (dt.datetime.now(dt.timezone.utc) - last_valid_soc_at).total_seconds()
                if last_valid_soc_at is not None
                else float("inf")
            )
            self._soc_control_ready = age_s <= SOC_BRIDGE_MAX_AGE_S
            return float(self._last_known_soc_pct)
        self._soc_control_ready = False
        return SOC_COLD_START_PLACEHOLDER_PCT

    def _grid_import_export(self) -> tuple[float, float]:
        mode = self.entry.data.get(CONF_GRID_MODE, GRID_MODE_THREE_PHASE)
        if mode == GRID_MODE_SIGNED:
            net = self._state_power_w(CONF_SIGNED_GRID_POWER_ENTITY)
            return max(0.0, net), max(0.0, -net)
        if mode == GRID_MODE_IMPORT_EXPORT:
            return self._state_power_w(CONF_GRID_IMPORT_ENTITY), self._state_power_w(
                CONF_GRID_EXPORT_ENTITY
            )
        if mode == GRID_MODE_THREE_PHASE:
            net = (
                self._state_power_w(CONF_GRID_L1_ENTITY)
                + self._state_power_w(CONF_GRID_L2_ENTITY)
                + self._state_power_w(CONF_GRID_L3_ENTITY)
            )
            return max(0.0, net), max(0.0, -net)
        return 0.0, 0.0

    def _battery_power_w(self) -> float:
        if self.entry.data.get(CONF_BATTERY_PROFILE) != BATTERY_PROFILE_ZENDURE:
            return self._state_power_w(CONF_BATTERY_POWER_ENTITY)

        ac_mode = self._state_value(CONF_ZENDURE_AC_MODE_ENTITY).lower()
        output_pack = self._state_power_w(CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY)
        pack_input = self._state_power_w(CONF_ZENDURE_PACK_INPUT_POWER_ENTITY)
        output_home = self._state_power_w(CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY)
        grid_input = self._state_power_w(CONF_ZENDURE_GRID_INPUT_POWER_ENTITY)

        if "input" in ac_mode:
            return -max(grid_input, output_pack, pack_input, output_home, 0.0)
        if "output" in ac_mode:
            return max(output_home, pack_input, grid_input, 0.0)
        return 0.0

    def _ev_power_w(self) -> float:
        """Return EV power, briefly bridging dropouts before blocking discharge."""
        entity_id = self.entry.data.get(CONF_EV_POWER_ENTITY)
        if not entity_id:
            self._ev_control_ready = True
            return 0.0
        state = self.hass.states.get(entity_id)
        if state is not None and state.state not in (
            "unknown",
            "unavailable",
            "none",
            "",
        ):
            try:
                value = max(0.0, self._raw_power_w(entity_id))
            except (TypeError, ValueError):
                value = None
            if value is not None:
                self._last_known_ev_power_w = value
                self._last_valid_ev_at = dt.datetime.now(dt.timezone.utc)
                self._ev_control_ready = True
                return value
        now = dt.datetime.now(dt.timezone.utc)
        if (
            self._last_valid_ev_at is not None
            and (now - self._last_valid_ev_at).total_seconds()
            <= EV_POWER_BRIDGE_MAX_AGE_S
        ):
            self._ev_control_ready = True
            return self._last_known_ev_power_w
        self._ev_control_ready = False
        return 0.0

    def _state_power_w(self, config_key: str, default: float = 0.0) -> float:
        """Return configured power normalized to watts."""
        entity_id = self.entry.data.get(config_key)
        if not entity_id:
            return default
        return self._raw_power_w(entity_id, default)

    def _raw_power_w(self, entity_id: str, default: float = 0.0) -> float:
        """Return a power entity in watts, preserving legacy unitless sensors."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "none", ""):
            return default
        try:
            raw = float(state.state)
        except (TypeError, ValueError):
            return default
        unit = str(state.attributes.get("unit_of_measurement") or "W").strip().lower()
        if unit == "kw":
            return raw * 1000.0
        if unit == "mw":
            return raw * 1_000_000.0
        return raw

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
        """Return a configured control entity, migrating older Zendure entries safely."""
        configured = self.entry.data.get(config_key)
        if configured:
            return configured
        ac_mode = self.entry.data.get(CONF_ZENDURE_AC_MODE_ENTITY, "")
        if ac_mode.endswith("_acmode"):
            base = ac_mode[: -len("_acmode")]
            if config_key == CONF_ZENDURE_INPUT_LIMIT_ENTITY:
                return f"{base}_inputlimit"
            if config_key == CONF_ZENDURE_OUTPUT_LIMIT_ENTITY:
                return f"{base}_outputlimit"
        return default

    def _state_age_s(self, entity_id: str) -> float:
        """Return seconds since a state changed."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return 1e9
        reported_at = (
            getattr(state, "last_reported", None)
            or getattr(state, "last_updated", None)
            or state.last_changed
        )
        return max(
            0.0, (dt.datetime.now(dt.timezone.utc) - reported_at).total_seconds()
        )

    def _state_available(self, entity_id: str) -> bool:
        """Return whether an entity has a usable state."""
        state = self.hass.states.get(entity_id)
        return state is not None and state.state not in (
            "unknown",
            "unavailable",
            "none",
            "",
        )

    def _grid_inputs_fresh(self) -> bool:
        """Return whether live grid inputs are fresh enough for real actuation."""
        mode = self.entry.data.get(CONF_GRID_MODE, GRID_MODE_THREE_PHASE)
        if mode == GRID_MODE_THREE_PHASE:
            entities = [
                self.entry.data.get(CONF_GRID_L1_ENTITY),
                self.entry.data.get(CONF_GRID_L2_ENTITY),
                self.entry.data.get(CONF_GRID_L3_ENTITY),
            ]
            return all(
                entity
                and self._state_available(entity)
                and self._state_age_s(entity) < GRID_INPUT_MAX_AGE_S
                for entity in entities
            )
        if mode == GRID_MODE_SIGNED:
            entity = self.entry.data.get(CONF_SIGNED_GRID_POWER_ENTITY)
            return bool(
                entity
                and self._state_available(entity)
                and self._state_age_s(entity) < GRID_INPUT_MAX_AGE_S
            )
        if mode == GRID_MODE_IMPORT_EXPORT:
            entities = [
                self.entry.data.get(CONF_GRID_IMPORT_ENTITY),
                self.entry.data.get(CONF_GRID_EXPORT_ENTITY),
            ]
            return all(
                entity
                and self._state_available(entity)
                and self._state_age_s(entity) < GRID_INPUT_MAX_AGE_S
                for entity in entities
            )
        return False

    async def _async_zero_limits_once(self, *, blocking: bool = False) -> bool:
        """Stop once when disabled; retry only until the safe stop can be issued."""
        self.last_actuation = await self._actuator.zero(
            "strategy_disabled", blocking=blocking, always_write=True
        )
        return self.last_actuation.get("status") != "skipped"

    async def _async_apply_command(
        self, command: StrategyCommand, options: StrategyOptions
    ) -> None:
        """Apply the command to Zendure with anti-oscillation guardrails."""
        if not self._soc_control_ready:
            await self._async_failsafe_zero_once("battery_soc_unavailable")
            return
        if not self._grid_inputs_fresh():
            await self._async_failsafe_zero_once("grid_inputs_stale")
            return
        if (
            command.mode == COMMAND_OUTPUT
            and self.entry.data.get(CONF_EV_POWER_ENTITY)
            and not self._ev_control_ready
            and (
                not options.battery_may_feed_ev
                or not options.discharge_during_ev_charging
            )
        ):
            await self._async_failsafe_zero_once("ev_power_unavailable")
            return
        self.last_actuation = await self._actuator.apply(command, options)

    async def _async_failsafe_zero_once(self, reason: str) -> None:
        """Zero both limits once while a safety-critical input is invalid."""
        self.last_actuation = await self._actuator.failsafe_zero_once(reason)

    async def async_prepare_unload(self) -> None:
        """Stop active output before a reload, then shut down the planner."""
        live_event_unsubs = getattr(self, "_live_event_unsubs", [])
        for unsubscribe in live_event_unsubs:
            unsubscribe()
        live_event_unsubs.clear()
        if self._strategy_was_enabled or bool(
            self.entry.options.get("strategy_enabled", False)
        ):
            stopped = await self._async_zero_limits_once(blocking=True)
            if not stopped:
                LOGGER.warning(
                    "Could not confirm safe battery stop before unloading Battery Strategy"
                )
        weather_task = getattr(self, "_weather_task", None)
        if weather_task is not None and not weather_task.done():
            weather_task.cancel()
            try:
                await weather_task
            except asyncio.CancelledError:
                pass
        await self._planner.async_shutdown()

    def _disabled_display_command(self, command: StrategyCommand) -> StrategyCommand:
        """Return the safe UI command while actuation is disabled."""
        return replace(
            command,
            mode=COMMAND_IDLE,
            power_w=0,
            reason="strategy_disabled_external_control",
            allowed_discharge_load_w=0,
        )

    def _append_command_trace(self, data: dict) -> None:
        """Append a compact command trace for later 12h/48h analysis."""
        path = Path(self.hass.config.path(COMMAND_TRACE_FILE))
        now = dt.datetime.now(dt.timezone.utc)
        command = data["command"]
        calculated_command = data["calculated_command"]
        plan = data["plan"]
        inputs = data["inputs"]
        directive = data["plan_to_live"]
        item = {
            "ts": now.timestamp(),
            "iso": now.isoformat(),
            "mode": command.mode,
            "power_w": command.power_w,
            "reason": command.reason,
            "calculated_mode": calculated_command.mode,
            "calculated_power_w": calculated_command.power_w,
            "calculated_reason": calculated_command.reason,
            "send_commands": data["send_commands"],
            "strategy_enabled": data["strategy_enabled"],
            "grid_import_w": round(inputs.grid_import_w),
            "grid_export_w": round(inputs.grid_export_w),
            "pv_w": round(inputs.pv_w),
            "battery_power_w": round(inputs.battery_power_w),
            "ev_power_w": round(inputs.ev_power_w),
            "soc_pct": round(inputs.soc_pct, 1),
            "soc_control_ready": data.get("soc_control_ready"),
            "soc_estimate_stale": data.get("soc_estimate_stale"),
            "current_plan_points": len(plan.points),
            "optimizer_age_s": data.get("optimizer_age_s"),
            "plan_mode": plan.current_mode,
            "plan_power_w": plan.current_power_w,
            "plan_to_live": asdict(directive),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")
        if path.stat().st_size > COMMAND_TRACE_MAX_BYTES:
            self._trim_command_trace(path)

    @staticmethod
    def _trim_command_trace(path: Path) -> None:
        """Bound trace disk usage without rewriting it during normal updates."""
        from collections import deque

        with path.open("r", encoding="utf-8") as handle:
            retained = deque(handle, maxlen=COMMAND_TRACE_RETAIN_LINES)
        tmp = path.with_suffix(f"{path.suffix}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.writelines(retained)
        tmp.replace(path)

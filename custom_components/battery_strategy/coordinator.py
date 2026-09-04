"""Data coordinator for Battery Strategy."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .actuator import HomeAssistantZendureActuator
from .command_trace import COMMAND_TRACE_FILE, append_command_trace
from .compiler_runtime import PlanCompilerRuntime
from .compiler_runtime_store import (
    CompilerRuntimeSnapshot,
    CompilerRuntimeStore,
)
from .config_definitions import option_default
from .const import (
    BATTERY_PROFILE_ZENDURE,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_INPUT_ENERGY_ENTITY,
    CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
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
    DISCHARGE_PRICE_SENSITIVE,
    DOMAIN,
    GRID_MODE_IMPORT_EXPORT,
    GRID_MODE_SIGNED,
    GRID_MODE_THREE_PHASE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    MANUAL_OFF,
)
from .contracts import (
    ActuationResult,
    AutomaticDischargeMode,
    BatteryCommand,
    CommandMode,
    DataQuality,
    ForecastRequest,
    LiveControlResult,
    LiveControlState,
    LiveMeasurements,
    LivePolicy,
    ManualControlMode,
    QualityFlag,
    SlotKey,
)
from .contracts.common import SLOT_MS
from .feature_store import (
    CompressedFeatureStore,
    ExecutorFeatureStore,
    FeatureAggregator,
    FeatureObservation,
)
from .live_control import P1UpdateGate
from .load_components import (
    LoadComponentCollection,
    add_central_weather,
    collect_load_components,
)
from .models import StrategyOptions
from .operator_projection import build_operator_projection
from .optimizer_state import last_known_soc_pct
from .planner import BackgroundPlanner
from .planning_adapter import PlanningPipelineAdapter
from .strategy import DeterministicLiveController
from .weather import OpenMeteoWeatherProvider

LOGGER = logging.getLogger(__name__)
OPTIMIZER_PREFETCH_LEAD_S = 60
SOC_BRIDGE_MAX_AGE_S = 300
SOC_COLD_START_PLACEHOLDER_PCT = 50.0
EV_POWER_BRIDGE_MAX_AGE_S = 180
OPTIMIZER_STATE_FILE = "battery_strategy_optimizer_state.json"
FEATURE_STORE_FILE = "battery_strategy_features.json.gz"
GRID_INPUT_MAX_AGE_S = 30
BATTERY_INPUT_MAX_AGE_S = 30
UNLOAD_STOP_TIMEOUT_S = 10.0
UNLOAD_STOP_POLL_S = 0.5


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
        compiler_runtime_store: CompilerRuntimeStore | None = None,
        restored_compiler_runtime: CompilerRuntimeSnapshot | None = None,
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
        self._planning_pipeline = PlanningPipelineAdapter(hass, entry)
        self._planning_pipeline.hydrate_output(last_optimizer_output)
        self._planner = BackgroundPlanner(
            hass, self._planning_pipeline, self.async_request_refresh
        )
        self._optimizer_attrs: Mapping[str, object] = {}
        self.last_actuation = ActuationResult(
            command_id="not-started",
            applied=False,
            applied_at_ms=0,
            detail="not_started",
        )
        self._compiler_runtime = PlanCompilerRuntime(restored_compiler_runtime)
        self._last_optimizer_force_key: str | None = None
        self._compiler_runtime_store = compiler_runtime_store
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
            min_command_delta_w=lambda: float(
                self.entry.options.get(
                    "min_command_delta_w", option_default("min_command_delta_w")
                )
            ),
        )
        self._p1_update_gate = P1UpdateGate()
        self._live_controller = DeterministicLiveController()
        self._live_control_state = LiveControlState(CommandMode.IDLE, 0.0, None)
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
        self._unloading = False
        self._actuation_lock = asyncio.Lock()

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

    async def _async_update_data(self):
        """Fetch current states and calculate command."""
        if getattr(self, "_unloading", False):
            return self.data or {}
        if (
            self._manual_until is not None
            and dt.datetime.now(dt.timezone.utc) >= self._manual_until
        ):
            self.clear_manual_override()

        options = self._strategy_options()
        now = dt.datetime.now(dt.timezone.utc)
        inputs = self._live_measurements(int(now.timestamp() * 1000))
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
        self._planning_pipeline.set_forecast_environment(
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
                    pv_generation_w=inputs.pv_generation_w,
                    battery_power_w=(
                        inputs.battery_discharge_w - inputs.battery_charge_w
                    ),
                    ev_charge_w=inputs.ev_charge_w,
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
        if QualityFlag.MISSING_BATTERY in inputs.quality.flags:
            self._compiler_runtime.suspend_accounting(now)
        else:
            measured_battery_power_w = (
                inputs.battery_discharge_w - inputs.battery_charge_w
            )
            self._compiler_runtime.account(now, measured_battery_power_w)
        force_optimizer = self._should_force_optimizer(now) or self._soc_recovered
        self._soc_recovered = False
        optimizer_scheduled = False
        if self._soc_control_ready:
            runtime_context = self._planning_pipeline.runtime_context(inputs, options)
            optimizer_scheduled = self._planner.maybe_schedule(
                inputs, options, runtime_context, force=force_optimizer
            )
        planning_result = self._planner.current(inputs, options)
        plan = planning_result.operator_plan
        self._optimizer_attrs = planning_result.operator_data
        now_ms = int(now.timestamp() * 1000)
        directive = self._compiler_runtime.compile(
            planning_result.battery_plan,
            options,
            inputs,
            now_ms,
            self._battery_energy_totals(),
        )
        if self._compiler_runtime.snapshot_dirty:
            await self._async_persist_compiler_runtime(clean_shutdown=False)
        strategy_enabled = bool(self.entry.options.get("strategy_enabled", False))
        control_state = (
            self._live_control_state
            if strategy_enabled
            else LiveControlState(CommandMode.IDLE, 0.0, None)
        )
        live_result = self._live_controller.command(
            directive,
            inputs,
            self._live_policy(options),
            control_state,
        )
        self._live_control_state = (
            live_result.state
            if strategy_enabled
            else LiveControlState(CommandMode.IDLE, 0.0, None)
        )
        calculated_command = live_result.command
        display_result = (
            live_result
            if strategy_enabled
            else self._disabled_display_result(live_result)
        )
        data = {
            "inputs": inputs,
            "options": options,
            "command": display_result.command,
            "calculated_command": calculated_command,
            "live_diagnostics": display_result.diagnostics,
            "plan": plan,
            "optimizer_attrs": self._optimizer_attrs,
            "plan_to_live": directive,
            "plan_compiler_error": self._compiler_runtime.error,
            "send_commands": strategy_enabled,
            "strategy_enabled": strategy_enabled,
            "actuation": asdict(self.last_actuation),
            "optimizer_age_s": self._planning_pipeline.age_s(),
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
            await self._async_apply_command(calculated_command)
            data["actuation"] = asdict(self.last_actuation)
        elif not self._disabled_zeroed:
            self._disabled_zeroed = await self._async_zero_limits_once()
            self._strategy_was_enabled = False
            data["actuation"] = asdict(self.last_actuation)
        else:
            self._strategy_was_enabled = False
            self.last_actuation = ActuationResult(
                command_id="disabled-no-write",
                applied=False,
                applied_at_ms=now_ms,
                detail="disabled_no_write",
            )
            data["actuation"] = asdict(self.last_actuation)
        if bool(self.entry.options.get("trace_enabled", False)):
            await self.hass.async_add_executor_job(
                append_command_trace,
                Path(self.hass.config.path(COMMAND_TRACE_FILE)),
                data,
            )
        if finalized_features:
            try:
                await self._feature_store.upsert(finalized_features)
                self._feature_history = await self._feature_store.load(
                    0, int(now.timestamp() * 1000) + 1
                )
                self._planning_pipeline.set_forecast_environment(
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
        data["operator_projection"] = build_operator_projection(
            data,
            local_date=local_now.date(),
            timezone=self.hass.config.time_zone,
        )
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
            self._planning_pipeline.set_forecast_environment(
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
            self._planning_pipeline.set_forecast_environment(
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
        if not self._soc_control_ready:
            flags.append(QualityFlag.ESTIMATED)
        if self.entry.data.get(CONF_EV_POWER_ENTITY) and not self._ev_control_ready:
            flags.append(QualityFlag.MISSING_EV)
        return tuple(flags)

    def _battery_measurement_available(self) -> bool:
        """Return whether the battery power reconstruction has usable inputs."""
        if self.entry.data.get(CONF_BATTERY_PROFILE) != BATTERY_PROFILE_ZENDURE:
            entity = self.entry.data.get(CONF_BATTERY_POWER_ENTITY)
            return bool(
                entity
                and self._state_available(entity)
                and self._state_age_s(entity) <= BATTERY_INPUT_MAX_AGE_S
            )
        mode_entity = self.entry.data.get(CONF_ZENDURE_AC_MODE_ENTITY)
        power_entities = (
            self.entry.data.get(CONF_ZENDURE_OUTPUT_PACK_POWER_ENTITY),
            self.entry.data.get(CONF_ZENDURE_PACK_INPUT_POWER_ENTITY),
            self.entry.data.get(CONF_ZENDURE_OUTPUT_HOME_POWER_ENTITY),
            self.entry.data.get(CONF_ZENDURE_GRID_INPUT_POWER_ENTITY),
        )
        return bool(
            mode_entity
            and self._state_available(mode_entity)
            and self._state_age_s(mode_entity) <= BATTERY_INPUT_MAX_AGE_S
            and all(
                entity
                and self._state_available(entity)
                and self._state_age_s(entity) <= BATTERY_INPUT_MAX_AGE_S
                for entity in power_entities
            )
        )

    def _should_force_optimizer(self, now: dt.datetime) -> bool:
        """Refresh planning once before and once after the active slot boundary."""
        slot_end_ms = self._compiler_runtime.active_slot_end_ms
        if slot_end_ms <= 0:
            return False
        now_ms = int(now.timestamp() * 1000)
        lead_ms = OPTIMIZER_PREFETCH_LEAD_S * 1000
        if now_ms < slot_end_ms - lead_ms:
            return False
        phase = "expired" if now_ms >= slot_end_ms else "prefetch"
        key = f"{self._compiler_runtime.active_slot_id}:{slot_end_ms}:{phase}"
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
            pv_charging=opts.get("pv_charging", option_default("pv_charging")),
            grid_charging=opts.get("grid_charging", option_default("grid_charging")),
            discharge=opts.get("discharge", option_default("discharge")),
            pv_to_ev_first=bool(
                opts.get("pv_to_ev_first", option_default("pv_to_ev_first"))
            ),
            discharge_during_ev_charging=bool(
                opts.get(
                    "discharge_during_ev_charging",
                    option_default("discharge_during_ev_charging"),
                )
            ),
            battery_may_feed_ev=bool(
                opts.get("battery_may_feed_ev", option_default("battery_may_feed_ev"))
            ),
            ev_active_threshold_w=float(
                opts.get(
                    "ev_active_threshold_w",
                    option_default("ev_active_threshold_w"),
                )
            ),
            min_soc_pct=float(opts.get("min_soc_pct", option_default("min_soc_pct"))),
            max_soc_pct=float(opts.get("max_soc_pct", option_default("max_soc_pct"))),
            max_charge_power_w=float(
                opts.get("max_charge_power_w", option_default("max_charge_power_w"))
            ),
            max_discharge_power_w=float(
                opts.get(
                    "max_discharge_power_w",
                    option_default("max_discharge_power_w"),
                )
            ),
            min_command_power_w=float(
                opts.get("min_command_power_w", option_default("min_command_power_w"))
            ),
            min_command_delta_w=float(
                opts.get("min_command_delta_w", option_default("min_command_delta_w"))
            ),
            round_trip_efficiency=float(
                opts.get(
                    "round_trip_efficiency",
                    option_default("round_trip_efficiency"),
                )
            ),
            min_margin_ct_per_kwh=float(
                opts.get(
                    "min_margin_ct_per_kwh",
                    option_default("min_margin_ct_per_kwh"),
                )
            ),
            planning_horizon_h=int(
                opts.get("planning_horizon_h", option_default("planning_horizon_h"))
            ),
            feed_in_tariff_ct_per_kwh=float(
                opts.get(
                    "feed_in_tariff_ct_per_kwh",
                    option_default("feed_in_tariff_ct_per_kwh"),
                )
            ),
            battery_capacity_kwh=float(
                opts.get(
                    CONF_BATTERY_CAPACITY_KWH,
                    option_default(CONF_BATTERY_CAPACITY_KWH),
                )
            ),
            pv_capacity_kwp=float(
                opts.get(CONF_PV_CAPACITY_KWP, option_default(CONF_PV_CAPACITY_KWP))
            ),
            pv_inverter_power_kw=float(
                opts.get(
                    CONF_PV_INVERTER_POWER_KW,
                    option_default(CONF_PV_INVERTER_POWER_KW),
                )
            ),
            manual_mode=manual_mode,
            manual_power_w=manual_power,
        )

    def _live_measurements(self, captured_at_ms: int) -> LiveMeasurements:
        grid_import, grid_export = self._grid_import_export()
        battery_power_w = self._battery_power_w()
        ev_power_w = self._ev_power_w()
        soc_pct = self._battery_soc_pct()
        quality_flags = self._feature_quality_flags()
        return LiveMeasurements(
            captured_at_ms=captured_at_ms,
            grid_import_w=grid_import,
            grid_export_w=grid_export,
            pv_generation_w=self._state_power_w(CONF_PV_POWER_ENTITY),
            battery_charge_w=max(0.0, -battery_power_w),
            battery_discharge_w=max(0.0, battery_power_w),
            ev_charge_w=ev_power_w,
            soc_pct=soc_pct,
            quality=DataQuality(
                coverage=0.0 if quality_flags else 1.0,
                flags=quality_flags,
            ),
        )

    @staticmethod
    def _live_policy(options: StrategyOptions) -> LivePolicy:
        discharge_mode = {
            DISCHARGE_LOAD: AutomaticDischargeMode.LOAD_FOLLOWING,
            DISCHARGE_PRICE_SENSITIVE: AutomaticDischargeMode.PRICE_SENSITIVE,
        }.get(options.discharge, AutomaticDischargeMode.OFF)
        manual_mode = {
            MANUAL_CHARGE: ManualControlMode.CHARGE,
            MANUAL_DISCHARGE: ManualControlMode.DISCHARGE,
        }.get(options.manual_mode, ManualControlMode.OFF)
        return LivePolicy(
            pv_to_ev_first=options.pv_to_ev_first,
            discharge_during_ev_charging=options.discharge_during_ev_charging,
            battery_may_feed_ev=options.battery_may_feed_ev,
            ev_active_threshold_w=options.ev_active_threshold_w,
            min_command_power_w=options.min_command_power_w,
            max_charge_power_w=options.max_charge_power_w,
            max_discharge_power_w=options.max_discharge_power_w,
            automatic_discharge_mode=discharge_mode,
            manual_mode=manual_mode,
            manual_power_w=options.manual_power_w,
        )

    def _battery_soc_pct(self) -> float:
        """Return the last real SoC estimate and gate control when it is stale."""
        entity_id = self.entry.data.get(CONF_BATTERY_SOC_ENTITY)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if (
                state is not None
                and self._state_age_s(entity_id) <= SOC_BRIDGE_MAX_AGE_S
                and state.state not in ("unknown", "unavailable", "none", "")
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
                    self._last_valid_soc_at = self._state_reported_at(entity_id)
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
        if (
            state is not None
            and self._state_age_s(entity_id) <= EV_POWER_BRIDGE_MAX_AGE_S
            and state.state not in ("unknown", "unavailable", "none", "")
        ):
            try:
                value = max(0.0, self._raw_power_w(entity_id))
            except (TypeError, ValueError):
                value = None
            if value is not None:
                self._last_known_ev_power_w = value
                self._last_valid_ev_at = self._state_reported_at(entity_id)
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
        reported_at = self._state_reported_at(entity_id)
        return max(
            0.0, (dt.datetime.now(dt.timezone.utc) - reported_at).total_seconds()
        )

    def _state_reported_at(self, entity_id: str) -> dt.datetime:
        """Return the source timestamp for one Home Assistant state."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        reported_at = (
            getattr(state, "last_reported", None)
            or getattr(state, "last_updated", None)
            or getattr(state, "last_changed", None)
        )
        if reported_at is None:
            # Home Assistant State always supplies timestamps. Lightweight test
            # doubles without them represent a freshly captured state.
            return dt.datetime.now(dt.timezone.utc)
        return reported_at

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
        async with self._get_actuation_lock():
            deadline = time.monotonic() + (UNLOAD_STOP_TIMEOUT_S if blocking else 0.0)
            while True:
                self.last_actuation = await self._actuator.apply(
                    self._safe_idle_command("strategy_disabled")
                )
                if self.last_actuation.detail == "control_entity_unavailable":
                    return False
                if not blocking or self._actuator.limits_zero_confirmed():
                    return True
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(UNLOAD_STOP_POLL_S)

    def _battery_energy_totals(self) -> tuple[float | None, float | None]:
        """Return cumulative battery charge/discharge counters when both are valid."""
        values = []
        for key in (
            CONF_BATTERY_INPUT_ENERGY_ENTITY,
            CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
        ):
            entity_id = self.entry.data.get(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            try:
                value = float(state.state)
            except (AttributeError, TypeError, ValueError):
                return None, None
            if value < 0.0 or state.state in ("unknown", "unavailable", "none", ""):
                return None, None
            values.append(value)
        return values[0], values[1]

    async def _async_persist_compiler_runtime(self, *, clean_shutdown: bool) -> None:
        """Persist one compact snapshot without making persistence a compiler concern."""
        store = self._compiler_runtime_store
        if store is None:
            return
        snapshot = self._compiler_runtime.storage_snapshot(
            saved_at_ms=int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
            energy_totals=self._battery_energy_totals(),
            clean_shutdown=clean_shutdown,
        )
        if snapshot is None:
            return
        try:
            await store.save(snapshot)
            self._compiler_runtime.mark_persisted()
        except (OSError, ValueError, TypeError) as err:
            LOGGER.warning("Could not persist active compiler progress: %s", err)

    async def _async_apply_command(
        self,
        command: BatteryCommand,
    ) -> None:
        """Pass the validated live command unchanged to the sole actuator."""
        async with self._get_actuation_lock():
            if getattr(self, "_unloading", False):
                self.last_actuation = ActuationResult(
                    command.command_id,
                    False,
                    int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
                    "unloading_no_write",
                )
                return
            self.last_actuation = await self._actuator.apply(command)

    def _get_actuation_lock(self) -> asyncio.Lock:
        """Return the per-entry lock shared by normal actuation and shutdown."""
        lock = getattr(self, "_actuation_lock", None)
        if lock is None:
            lock = self._actuation_lock = asyncio.Lock()
        return lock

    @staticmethod
    def _safe_idle_command(reason: str) -> BatteryCommand:
        """Create a short-lived generic zero command for disable and fail-safe paths."""
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        return BatteryCommand(
            command_id=f"safety:{reason}:{now_ms}",
            directive_id="safety",
            created_at_ms=now_ms,
            valid_until_ms=now_ms + 30_000,
            mode=CommandMode.IDLE,
            power_w=0.0,
            reason=reason,
        )

    async def async_prepare_unload(self) -> bool:
        """Stop active output before a reload, then shut down the planner."""
        self._unloading = True
        self._planner.begin_shutdown()
        if self._strategy_was_enabled or bool(
            self.entry.options.get("strategy_enabled", False)
        ):
            try:
                stopped = await self._async_zero_limits_once(blocking=True)
            except Exception:  # noqa: BLE001 - a rejected unload must restore control.
                self._unloading = False
                self._planner.abort_shutdown()
                LOGGER.exception(
                    "Battery stop failed; refusing Battery Strategy unload"
                )
                return False
            if not stopped:
                self._unloading = False
                self._planner.abort_shutdown()
                LOGGER.error(
                    "Refusing to unload Battery Strategy before battery stop is confirmed"
                )
                return False
        live_event_unsubs = getattr(self, "_live_event_unsubs", [])
        for unsubscribe in live_event_unsubs:
            unsubscribe()
        live_event_unsubs.clear()
        await self._async_persist_compiler_runtime(clean_shutdown=True)
        weather_task = getattr(self, "_weather_task", None)
        if weather_task is not None and not weather_task.done():
            weather_task.cancel()
            try:
                await weather_task
            except asyncio.CancelledError:
                pass
        await self._planner.async_shutdown()
        return True

    async def async_abort_unload(self) -> None:
        """Restore retained runtime ownership after platform unload rejection."""
        await self._async_persist_compiler_runtime(clean_shutdown=False)
        self._unloading = False
        self._planner.abort_shutdown()
        self._weather_task = None
        self._weather_refresh_key = None
        self.async_start_live_tracking()

    def _disabled_display_result(self, result: LiveControlResult) -> LiveControlResult:
        """Return the safe UI command while actuation is disabled."""
        command = replace(
            result.command,
            command_id=f"{result.command.command_id}:disabled",
            mode=CommandMode.IDLE,
            power_w=0.0,
            reason="strategy_disabled_external_control",
        )
        return replace(
            result,
            command=command,
            state=LiveControlState(CommandMode.IDLE, 0.0, command.created_at_ms),
            diagnostics=replace(result.diagnostics, allowed_discharge_load_w=0.0),
        )

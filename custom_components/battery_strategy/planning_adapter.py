"""Home Assistant adapter for the planning application."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import replace
from zoneinfo import ZoneInfo

from homeassistant.helpers import entity_registry as er

from .component_config import LoadComponentSpec
from .const import (
    CONF_BATTERY_INPUT_ENERGY_ENTITY,
    CONF_BATTERY_OUTPUT_ENERGY_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PV_POWER_ENTITY,
    DOMAIN,
)
from .contracts import (
    HistoricalFeatureSlot,
    LiveMeasurements,
    LoadDriverSnapshot,
    LoadForecastContext,
    WeatherSlot,
)
from .history_adapter import read_recorder_series
from .models import StrategyOptions
from .planning_result import (
    PlanningResult,
    persisted_output,
    result_from_persisted_output,
)
from .planning_state import PLANNING_STATE_FILENAME, PlanningStateStore

CACHE_TTL_S = 240
SLOT_MS = 15 * 60 * 1000
_PLANNING_RUN_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)


class PlanningPipelineAdapter:
    """Adapt Home Assistant snapshots to the planning application pipeline."""

    def __init__(
        self,
        hass=None,
        entry=None,
        state_store: PlanningStateStore | None = None,
    ) -> None:
        """Initialize adapter cache."""
        self._hass = hass
        self._entry = entry
        self._last_run_monotonic = 0.0
        self._last_output: dict | None = None
        self._last_result: PlanningResult | None = None
        self._last_options: StrategyOptions | None = None
        self._state_store = state_store or (
            PlanningStateStore.claim(
                f"{hass.config.config_dir.rstrip('/')}/{PLANNING_STATE_FILENAME}"
            )
            if hass is not None
            else None
        )
        try:
            self._timezone = (
                ZoneInfo(str(hass.config.time_zone)) if hass else dt.timezone.utc
            )
        except (KeyError, ValueError):
            self._timezone = dt.timezone.utc
        self._forecast_history: tuple[HistoricalFeatureSlot, ...] = ()
        self._forecast_weather: tuple[WeatherSlot, ...] = ()
        self._forecast_drivers: tuple[LoadDriverSnapshot, ...] = ()
        self._forecast_component_specs: tuple[LoadComponentSpec, ...] = ()

    def set_forecast_environment(
        self, history=(), weather=(), drivers=(), component_specs=()
    ) -> None:
        """Replace immutable feature inputs consumed by the next plan run."""
        self._forecast_history = tuple(history)
        self._forecast_weather = tuple(weather)
        self._forecast_drivers = tuple(drivers)
        self._forecast_component_specs = tuple(component_specs)

    def hydrate_output(self, output: dict | None) -> None:
        """Hydrate the cache from an already loaded startup snapshot."""
        if output:
            self._last_output = dict(output)
            self._last_result = None

    def cached_result(
        self, inputs: LiveMeasurements, options: StrategyOptions
    ) -> PlanningResult:
        """Return the cached plan without running the optimizer."""
        if self._last_result is None:
            self._last_result = result_from_persisted_output(
                self._last_output or {},
                options,
                timezone=self._timezone,
                now_ms=inputs.captured_at_ms,
            )
            # The persistence codec has already verified this exact policy.
            # Remember it so a later option change invalidates the cached plan.
            self._last_options = options
        result = self._last_result
        override_active = options.manual_mode != "off"
        if self._last_options is not None and self._last_options != options:
            # A stale plan may violate newly lowered physical limits. Preserve
            # only its operator projection until the forced refresh completes.
            result = replace(
                result,
                battery_plan=None,
                operator_plan=replace(
                    result.operator_plan, override_active=override_active
                ),
            )
        elif result.operator_plan.override_active != override_active:
            result = replace(
                result,
                operator_plan=replace(
                    result.operator_plan, override_active=override_active
                ),
            )
        return result

    def needs_run(self, options: StrategyOptions, *, force: bool = False) -> bool:
        """Return whether the background planner should refresh."""
        return bool(
            force
            or self._last_output is None
            or self._last_options != options
            or time.monotonic() - self._last_run_monotonic >= CACHE_TTL_S
        )

    def run(
        self,
        inputs: LiveMeasurements,
        options: StrategyOptions,
        force: bool = False,
        runtime_context: dict | None = None,
    ) -> PlanningResult:
        """Return a typed result with one canonical executable plan."""
        if runtime_context and runtime_context.get("timezone"):
            try:
                self._timezone = ZoneInfo(str(runtime_context["timezone"]))
            except (KeyError, ValueError):
                self._timezone = dt.timezone.utc
        cache_now = time.monotonic()
        if (
            not force
            and self._last_output is not None
            and self._last_options == options
            and cache_now - self._last_run_monotonic < CACHE_TTL_S
        ):
            return self.cached_result(inputs, options)

        from . import planning_pipeline

        # Executor work survives config-entry cancellation. Serialize persistence
        # so an old and a new coordinator cannot write the same state file together.
        with _PLANNING_RUN_LOCK:
            if runtime_context:
                runtime_context = dict(runtime_context)
                if self._hass is not None:
                    try:
                        captured_at = dt.datetime.fromtimestamp(
                            int(runtime_context["captured_at_ms"]) / 1000.0,
                            tz=dt.timezone.utc,
                        )
                        runtime_context["history_series"] = read_recorder_series(
                            self._hass,
                            runtime_context.get("entity_map") or {},
                            runtime_context.get("entity_scale") or {},
                            start_time=captured_at - dt.timedelta(hours=49),
                            end_time=captured_at,
                        )
                    except Exception as err:  # Recorder failure must not stop control.
                        LOGGER.warning("Recorder history snapshot failed: %s", err)
                        runtime_context["history_series"] = {}
            runtime = planning_pipeline.PlanningRuntime.from_mapping(runtime_context)
            try:
                result = planning_pipeline.run(runtime)
            except planning_pipeline.StalePlanningResult:
                # A newer lifecycle already persisted a result. Treat this run as
                # observed so completion refreshes cannot create a retry loop.
                fallback = self.cached_result(inputs, options)
                self._last_result = fallback
                self._last_options = options
                self._last_run_monotonic = time.monotonic()
                return fallback
        self._last_result = result
        self._last_output = persisted_output(result, options)
        self._last_options = options
        self._last_run_monotonic = cache_now
        return self.cached_result(inputs, options)

    def runtime_context(
        self, inputs: LiveMeasurements, options: StrategyOptions
    ) -> dict:
        """Snapshot HA-owned runtime data before entering the executor thread."""
        if self._hass is None or self._entry is None:
            return {}
        data = self._entry.data
        price_entity = data.get(CONF_PRICE_ENTITY)
        price_state = self._hass.states.get(price_entity) if price_entity else None
        price_intervals = (
            list(price_state.attributes.get("data") or []) if price_state else []
        )
        battery_power_w = inputs.battery_discharge_w - inputs.battery_charge_w
        states = {
            "grid_import": inputs.grid_import_w,
            "grid_export": inputs.grid_export_w,
            "pv_power": inputs.pv_generation_w,
            "battery_soc": inputs.soc_pct,
            "battery_min_soc": options.min_soc_pct,
            "battery_power": battery_power_w,
            "ev_power": inputs.ev_charge_w,
            "ev_status": "charging"
            if inputs.ev_charge_w >= options.ev_active_threshold_w
            else "idle",
        }
        house_load_no_ev_w = max(
            0.0,
            inputs.grid_import_w
            + inputs.pv_generation_w
            + battery_power_w
            - inputs.grid_export_w
            - inputs.ev_charge_w,
        )
        if price_state is not None:
            states["price_current"] = price_state.state
        registry = er.async_get(self._hass)
        grid_import_entity = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self._entry.entry_id}_grid_import"
        )
        grid_export_entity = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self._entry.entry_id}_grid_export"
        )
        battery_power_entity = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self._entry.entry_id}_battery_power"
        )
        ev_entity = data.get(CONF_EV_POWER_ENTITY)
        pv_entity = data.get(CONF_PV_POWER_ENTITY)
        return {
            "captured_at_ms": int(inputs.captured_at_ms),
            "state_store": self._state_store,
            "config_dir": self._hass.config.config_dir,
            "latitude": self._hass.config.latitude,
            "longitude": self._hass.config.longitude,
            "timezone": self._hass.config.time_zone,
            "states": states,
            "price_intervals": price_intervals,
            "entity_map": {
                "price_current": price_entity,
                "price_eur": price_entity,
                "grid_import": grid_import_entity,
                "grid_export": grid_export_entity,
                "pv_power": pv_entity,
                "battery_soc": data.get(CONF_BATTERY_SOC_ENTITY),
                "battery_input_energy": data.get(CONF_BATTERY_INPUT_ENERGY_ENTITY),
                "battery_output_energy": data.get(CONF_BATTERY_OUTPUT_ENERGY_ENTITY),
                "battery_power": battery_power_entity,
                "ev_power": ev_entity,
            },
            "entity_scale": {
                "pv_power": self._power_scale(pv_entity),
                "ev_power": self._power_scale(ev_entity),
            },
            "battery_capacity_kwh": options.battery_capacity_kwh,
            "min_soc_pct": options.min_soc_pct,
            "max_soc_pct": options.max_soc_pct,
            "max_power_w": max(
                options.max_charge_power_w, options.max_discharge_power_w
            ),
            "max_charge_power_w": options.max_charge_power_w,
            "max_discharge_power_w": options.max_discharge_power_w,
            "round_trip_efficiency": options.round_trip_efficiency,
            "min_margin_ct_per_kwh": options.min_margin_ct_per_kwh,
            "feed_in_tariff_ct_per_kwh": options.feed_in_tariff_ct_per_kwh,
            "pv_capacity_kwp": options.pv_capacity_kwp,
            "pv_inverter_power_kw": options.pv_inverter_power_kw,
            "pv_charging": options.pv_charging,
            "grid_charging": options.grid_charging,
            "discharge": options.discharge,
            "planning_horizon_h": options.planning_horizon_h,
            "forecast_history": self._forecast_history,
            "forecast_weather": self._forecast_weather,
            "forecast_context": LoadForecastContext(
                house_load_no_ev_w, self._forecast_drivers
            ),
            "forecast_component_specs": self._forecast_component_specs,
        }

    def _power_scale(self, entity_id: str | None) -> float:
        """Return the recorder-history scale needed to normalize power to watts."""
        state = self._hass.states.get(entity_id) if entity_id else None
        unit = (
            str(
                state.attributes.get("unit_of_measurement")
                if state is not None
                else "W"
            )
            .strip()
            .lower()
        )
        return {"kw": 1000.0, "mw": 1_000_000.0}.get(unit, 1.0)

    def age_s(self) -> float | None:
        """Return seconds since the last optimizer run."""
        if self._last_run_monotonic <= 0.0:
            return None
        return max(0.0, time.monotonic() - self._last_run_monotonic)

    def revoke_state_writer(self) -> None:
        """Close state publication for this unloaded coordinator generation."""
        if self._state_store is not None:
            self._state_store.revoke()

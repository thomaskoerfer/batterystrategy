"""Home Assistant adapter for the planning application."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
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
from .planning_runtime import (
    HistoryRole,
    PlanningHistory,
    PlanningObservations,
    PlanningRuntime,
    PlanningRuntimeSettings,
)
from .planning_state import PLANNING_STATE_FILENAME, PlanningStateStore
from .runtime_market_data import TariffSchedule

CACHE_TTL_S = 240
SLOT_MS = 15 * 60 * 1000
_PLANNING_RUN_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)


def _as_float(value) -> float | None:
    if value in (None, "unknown", "unavailable", "none", ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PlanningCapture:
    """Private adapter capture awaiting executor-owned Recorder history."""

    snapshot: PlanningRuntime
    recorder_entities: Mapping[HistoryRole, str]
    recorder_scales: Mapping[HistoryRole, float]


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
        runtime_context: PlanningCapture | None = None,
    ) -> PlanningResult:
        """Return a typed result with one canonical executable plan."""
        if runtime_context is not None:
            self._timezone = runtime_context.snapshot.settings.timezone
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
            if runtime_context is None or self._state_store is None:
                raise RuntimeError("planning capture and state store are required")
            runtime = runtime_context.snapshot
            if self._hass is not None:
                try:
                    captured_at = dt.datetime.fromtimestamp(
                        runtime.captured_at_s, tz=dt.timezone.utc
                    )
                    series = read_recorder_series(
                        self._hass,
                        dict(runtime_context.recorder_entities),
                        dict(runtime_context.recorder_scales),
                        start_time=captured_at - dt.timedelta(hours=49),
                        end_time=captured_at,
                    )
                    runtime = runtime.with_history(
                        PlanningHistory.from_series(
                            series, captured_at_s=runtime.captured_at_s
                        )
                    )
                except Exception as err:  # Recorder failure must not stop control.
                    LOGGER.warning("Recorder history snapshot failed: %s", err)
            owner_state = self._state_store.load(
                runtime.settings, runtime.captured_at_ms
            )
            try:
                outcome = planning_pipeline.run(runtime, owner_state)
                if outcome.persist_state and not self._state_store.save(
                    outcome.owner_state
                ):
                    raise planning_pipeline.StalePlanningResult(
                        "newer planning result already persisted"
                    )
                result = outcome.result
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
    ) -> PlanningCapture:
        """Snapshot HA-owned runtime data before entering the executor thread."""
        if self._hass is None or self._entry is None:
            raise RuntimeError("Home Assistant and config entry are required")
        data = self._entry.data
        price_entity = data.get(CONF_PRICE_ENTITY)
        price_state = self._hass.states.get(price_entity) if price_entity else None
        provider_prices = (
            list(price_state.attributes.get("data") or []) if price_state else []
        )
        settings = PlanningRuntimeSettings.from_options(options, self._timezone)
        tariffs = TariffSchedule.from_provider_rows(provider_prices, settings.timezone)
        current_price = tariffs.price_eur_at(inputs.captured_at_ms / 1000.0)
        current_price_ct = current_price * 100.0 if current_price is not None else None
        if current_price_ct is None and price_state is not None:
            current_price_ct = _as_float(price_state.state)
        local_now = dt.datetime.fromtimestamp(
            inputs.captured_at_ms / 1000.0, dt.timezone.utc
        ).astimezone(settings.timezone)
        future_stats = tariffs.future_price_stats(local_now)
        future_max_price_ct = (
            future_stats["max_ct"] if future_stats is not None else current_price_ct
        )
        battery_power_w = inputs.battery_discharge_w - inputs.battery_charge_w
        house_load_no_ev_w = max(
            0.0,
            inputs.grid_import_w
            + inputs.pv_generation_w
            + battery_power_w
            - inputs.grid_export_w
            - inputs.ev_charge_w,
        )
        current_weather = next(
            (
                item
                for item in self._forecast_weather
                if item.slot.start_ms <= inputs.captured_at_ms < item.slot.end_ms
            ),
            self._forecast_weather[0] if self._forecast_weather else None,
        )
        cloud_cover = (
            current_weather.cloud_cover_pct
            if current_weather is not None
            and current_weather.cloud_cover_pct is not None
            else 50.0
        )
        radiation = (
            current_weather.shortwave_radiation_w_m2
            if current_weather is not None
            and current_weather.shortwave_radiation_w_m2 is not None
            else 0.0
        )
        observations = PlanningObservations(
            current_price_ct_per_kwh=current_price_ct,
            future_max_price_ct_per_kwh=future_max_price_ct,
            grid_import_w=float(inputs.grid_import_w),
            grid_export_w=float(inputs.grid_export_w),
            pv_generation_w=float(inputs.pv_generation_w),
            battery_power_w=float(battery_power_w),
            battery_soc_pct=float(inputs.soc_pct),
            battery_min_soc_pct=float(options.min_soc_pct),
            ev_charge_w=(
                float(inputs.ev_charge_w)
                if inputs.ev_charge_w >= options.ev_active_threshold_w
                else 0.0
            ),
            heat_pump_power_w=0.0,
            pv_next_hour_kwh=0.0,
            pv_tomorrow_kwh=None,
            cloud_cover_pct=float(cloud_cover),
            shortwave_radiation_w_m2=float(radiation),
        )
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
        recorder_entities = {
            HistoryRole.PRICE_EUR: price_entity,
            HistoryRole.GRID_IMPORT: grid_import_entity,
            HistoryRole.GRID_EXPORT: grid_export_entity,
            HistoryRole.PV_POWER: pv_entity,
            HistoryRole.BATTERY_SOC: data.get(CONF_BATTERY_SOC_ENTITY),
            HistoryRole.BATTERY_INPUT_ENERGY: data.get(
                CONF_BATTERY_INPUT_ENERGY_ENTITY
            ),
            HistoryRole.BATTERY_OUTPUT_ENERGY: data.get(
                CONF_BATTERY_OUTPUT_ENERGY_ENTITY
            ),
            HistoryRole.BATTERY_POWER: battery_power_entity,
            HistoryRole.EV_POWER: ev_entity,
        }
        snapshot = PlanningRuntime(
            captured_at_ms=int(inputs.captured_at_ms),
            settings=settings,
            observations=observations,
            history=PlanningHistory.empty(),
            tariffs=tariffs,
            forecast_history=self._forecast_history,
            forecast_weather=self._forecast_weather,
            forecast_context=LoadForecastContext(
                house_load_no_ev_w, self._forecast_drivers
            ),
            forecast_component_specs=self._forecast_component_specs,
        )
        return PlanningCapture(
            snapshot=snapshot,
            recorder_entities=MappingProxyType(
                {key: value for key, value in recorder_entities.items() if value}
            ),
            recorder_scales=MappingProxyType(
                {
                    HistoryRole.PV_POWER: self._power_scale(pv_entity),
                    HistoryRole.EV_POWER: self._power_scale(ev_entity),
                }
            ),
        )

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

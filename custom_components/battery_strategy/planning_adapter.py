"""Home Assistant adapter for the planning application."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo

from homeassistant.helpers import entity_registry as er

from .component_config import LoadComponentSpec
from .const import (
    COMMAND_IDLE,
    COMMAND_INPUT,
    COMMAND_OUTPUT,
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
    LoadDriverSnapshot,
    LoadForecastContext,
    WeatherSlot,
)
from .history_adapter import read_recorder_series
from .models import StrategyInputs, StrategyOptions
from .plan_models import DailyCost, PlanPoint, StrategyPlan

CACHE_TTL_S = 240
SLOT_MS = 15 * 60 * 1000
_PLANNING_RUN_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)


class PlanningPipelineAdapter:
    """Adapt Home Assistant snapshots to the planning application pipeline."""

    def __init__(self, hass=None, entry=None) -> None:
        """Initialize adapter cache."""
        self._hass = hass
        self._entry = entry
        self._last_run_ts = 0.0
        self._last_output: dict | None = None
        self._last_options: StrategyOptions | None = None
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

    def cached_result(
        self, inputs: StrategyInputs, options: StrategyOptions
    ) -> tuple[StrategyPlan, dict]:
        """Return the cached plan without running the optimizer."""
        output = self._last_output or {}
        return _plan_from_output(output, inputs, options, self._timezone), output

    def needs_run(self, options: StrategyOptions, *, force: bool = False) -> bool:
        """Return whether the background planner should refresh."""
        return bool(
            force
            or self._last_output is None
            or self._last_options != options
            or time.time() - self._last_run_ts >= CACHE_TTL_S
        )

    def run(
        self,
        inputs: StrategyInputs,
        options: StrategyOptions,
        force: bool = False,
        runtime_context: dict | None = None,
    ) -> tuple[StrategyPlan, dict]:
        """Return a high-quality plan and raw optimizer attributes."""
        if runtime_context and runtime_context.get("timezone"):
            try:
                self._timezone = ZoneInfo(str(runtime_context["timezone"]))
            except (KeyError, ValueError):
                self._timezone = dt.timezone.utc
        now = time.time()
        if (
            not force
            and self._last_output is not None
            and self._last_options == options
            and now - self._last_run_ts < CACHE_TTL_S
        ):
            return _plan_from_output(
                self._last_output, inputs, options, self._timezone
            ), self._last_output

        from . import planning_pipeline

        # Executor work survives config-entry cancellation. Serialize persistence
        # so an old and a new coordinator cannot write the same state file together.
        with _PLANNING_RUN_LOCK:
            if runtime_context:
                runtime_context = dict(runtime_context)
                if self._hass is not None:
                    try:
                        runtime_context["history_series"] = read_recorder_series(
                            self._hass,
                            runtime_context.get("entity_map") or {},
                            runtime_context.get("entity_scale") or {},
                            start_time=dt.datetime.now(dt.timezone.utc)
                            - dt.timedelta(hours=49),
                        )
                    except Exception as err:  # Recorder failure must not stop control.
                        LOGGER.warning("Recorder history snapshot failed: %s", err)
                        runtime_context["history_series"] = {}
            output = planning_pipeline.run(runtime_context)
        self._last_output = output
        self._last_options = options
        self._last_run_ts = now
        return _plan_from_output(output, inputs, options, self._timezone), output

    def runtime_context(self, inputs: StrategyInputs, options: StrategyOptions) -> dict:
        """Snapshot HA-owned runtime data before entering the executor thread."""
        if self._hass is None or self._entry is None:
            return {}
        data = self._entry.data
        price_entity = data.get(CONF_PRICE_ENTITY)
        price_state = self._hass.states.get(price_entity) if price_entity else None
        price_intervals = (
            list(price_state.attributes.get("data") or []) if price_state else []
        )
        states = {
            "grid_import": inputs.grid_import_w,
            "grid_export": inputs.grid_export_w,
            "pv_power": inputs.pv_w,
            "battery_soc": inputs.soc_pct,
            "battery_min_soc": options.min_soc_pct,
            "battery_power": inputs.battery_power_w,
            "ev_power": inputs.ev_power_w,
            "ev_status": "charging"
            if inputs.ev_power_w >= options.ev_active_threshold_w
            else "idle",
        }
        house_load_no_ev_w = max(
            0.0,
            inputs.grid_import_w
            + inputs.pv_w
            + inputs.battery_power_w
            - inputs.grid_export_w
            - inputs.ev_power_w,
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
        if self._last_run_ts <= 0.0:
            return None
        return max(0.0, time.time() - self._last_run_ts)


def _plan_from_output(
    output: dict,
    inputs: StrategyInputs,
    options: StrategyOptions,
    timezone: dt.tzinfo = dt.timezone.utc,
) -> StrategyPlan:
    points = _points_from_output(output, timezone=timezone)
    today = _date_from_points(points, 0)
    tomorrow = _date_from_points(points, 1)
    daily_costs = {}
    if today:
        daily_costs[today] = DailyCost(
            _float(output.get("baseline_cost_today_eur")),
            _float(output.get("optimized_cost_today_eur")),
        )
    if tomorrow:
        daily_costs[tomorrow] = DailyCost(
            _float(output.get("baseline_cost_tomorrow_eur")),
            _float(output.get("optimized_cost_tomorrow_eur")),
        )
    mode = _mode_to_command(
        str(output.get("mode") or output.get("planned_mode") or "idle")
    )
    return StrategyPlan(
        points=points,
        current_mode=mode,
        current_power_w=int(
            round(
                _float(
                    output.get("recommended_power_w", output.get("planned_power_w", 0))
                )
            )
        ),
        reason=str(output.get("reason") or "planning_pipeline"),
        daily_costs=daily_costs,
        price_stats={
            "min": _maybe_float(output.get("price_min_ct")),
            "max": _maybe_float(output.get("price_max_ct")),
            "avg": _maybe_float(output.get("price_avg_ct")),
            "p_low": _maybe_float(output.get("price_low_ct")),
            "p_high": _maybe_float(output.get("price_high_ct")),
            "terminal_value_ct": _maybe_float(output.get("terminal_value_ct")),
            "discharge_floor_ct": _maybe_float(output.get("discharge_floor_ct")),
        },
        load_forecast_next_1h_kwh=_float(output.get("load_forecast_next_1h_kwh")),
        pv_forecast_corrected_next_1h_kwh=_float(
            output.get("pv_forecast_corrected_next_1h_kwh")
        ),
        net_load_forecast_next_1h_kwh=_float(
            output.get("net_load_forecast_next_1h_kwh")
        ),
        grid_import_forecast_next_1h_kwh=_float(
            output.get("grid_import_forecast_next_1h_kwh")
        ),
        grid_export_forecast_next_1h_kwh=_float(
            output.get("grid_export_forecast_next_1h_kwh")
        ),
        virtual_soc_end_tomorrow_pct=_float(output.get("virtual_soc_end_tomorrow_pct")),
        override_active=options.manual_mode != "off",
    )


def _points_from_output(
    output: dict,
    now_ms: int | None = None,
    timezone: dt.tzinfo = dt.timezone.utc,
) -> list[PlanPoint]:
    price = _series(output.get("profile_48h_price")) or _merge_series(
        _series(output.get("profile_today_price")),
        _series(output.get("profile_tomorrow_price")),
    )
    soc = _merge_series(
        _series(output.get("profile_today_soc")),
        _series(output.get("profile_tomorrow_soc")),
    )
    power = _merge_series(
        _series(output.get("profile_today_power")),
        _series(output.get("profile_tomorrow_power")),
    )
    charge = _series(output.get("profile_48h_charge_fc_power")) or _merge_series(
        _series(output.get("profile_today_charge_power")),
        _series(output.get("profile_tomorrow_charge_power")),
    )
    pv_charge = _series(output.get("profile_48h_pv_charge_fc_power")) or _merge_series(
        _series(output.get("profile_today_pv_charge_power")),
        _series(output.get("profile_tomorrow_pv_charge_power")),
    )
    grid_charge = _series(
        output.get("profile_48h_grid_charge_fc_power")
    ) or _merge_series(
        _series(output.get("profile_today_grid_charge_power")),
        _series(output.get("profile_tomorrow_grid_charge_power")),
    )
    required_charge = _series(
        output.get("profile_48h_required_charge_fc_power")
    ) or _merge_series(
        _series(output.get("profile_today_required_charge_power")),
        _series(output.get("profile_tomorrow_required_charge_power")),
    )
    discharge = _series(output.get("profile_48h_discharge_fc_power")) or _merge_series(
        _series(output.get("profile_today_discharge_power")),
        _series(output.get("profile_tomorrow_discharge_power")),
    )
    discharge_budget = _series(
        output.get("profile_48h_discharge_budget_kwh")
    ) or _merge_series(
        _series(output.get("profile_today_discharge_budget_kwh")),
        _series(output.get("profile_tomorrow_discharge_budget_kwh")),
    )
    pv = _series(output.get("profile_48h_pv_fc_power"))
    load = _series(output.get("profile_48h_house_fc_power"))
    grid_import = _series(output.get("profile_48h_grid_import_fc_power"))
    grid_export = _series(output.get("profile_48h_grid_export_fc_power"))
    grid_net = _series(output.get("profile_48h_grid_net_fc_power"))
    ts_values = sorted(
        {
            *load,
            *pv,
            *grid_import,
            *grid_export,
            *grid_net,
            *charge,
            *pv_charge,
            *grid_charge,
            *required_charge,
            *discharge,
            *soc,
            *power,
        }
    )
    points = []
    for ts_ms in ts_values:
        ch = _at(charge, ts_ms)
        dis = _at(discharge, ts_ms)
        forecast_surplus_w = max(0.0, _at(pv, ts_ms) - _at(load, ts_ms))
        explicit_sources = bool(pv_charge or grid_charge or required_charge)
        planned_pv_charge_w = (
            _at(pv_charge, ts_ms) if explicit_sources else min(ch, forecast_surplus_w)
        )
        planned_grid_charge_w = (
            _at(grid_charge, ts_ms)
            if explicit_sources
            else max(0.0, ch - planned_pv_charge_w)
        )
        required_charge_w = (
            _at(required_charge, ts_ms)
            if explicit_sources
            else (ch if planned_grid_charge_w > 0.0 else 0.0)
        )
        pow_w = _at(power, ts_ms) or max(ch, dis)
        mode = COMMAND_INPUT if ch > 0 else COMMAND_OUTPUT if dis > 0 else COMMAND_IDLE
        slot_dt = dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone)
        points.append(
            PlanPoint(
                ts_ms=ts_ms,
                date=slot_dt.date().isoformat(),
                price_ct=_at(price, ts_ms),
                load_fc_w=int(round(_at(load, ts_ms))),
                pv_fc_w=int(round(_at(pv, ts_ms))),
                grid_import_fc_w=int(round(_at(grid_import, ts_ms))),
                grid_export_fc_w=int(round(_at(grid_export, ts_ms))),
                grid_net_fc_w=int(round(_at(grid_net, ts_ms))),
                mode=mode,
                power_w=int(round(abs(pow_w))),
                charge_fc_w=int(round(ch)),
                discharge_fc_w=int(round(dis)),
                soc_pct=round(_at(soc, ts_ms), 2),
                discharge_budget_kwh=round(_at(discharge_budget, ts_ms), 3),
                pv_charge_fc_w=int(round(planned_pv_charge_w)),
                grid_charge_fc_w=int(round(planned_grid_charge_w)),
                required_charge_fc_w=int(round(required_charge_w)),
            )
        )
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return [point for point in points if point.ts_ms + SLOT_MS > now_ms]


def _series(raw) -> dict[int, float]:
    result = {}
    for item in raw or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            result[int(float(item[0]))] = float(item[1])
        except (TypeError, ValueError):
            continue
    return result


def _merge_series(*series_items: dict[int, float]) -> dict[int, float]:
    merged: dict[int, float] = {}
    for series in series_items:
        merged.update(series)
    return merged


def _at(series: dict[int, float], ts_ms: int) -> float:
    if ts_ms in series:
        return float(series[ts_ms])
    if not series:
        return 0.0
    best = min(series, key=lambda item_ts: abs(item_ts - ts_ms))
    return float(series[best]) if abs(best - ts_ms) <= 20 * 60 * 1000 else 0.0


def _date_from_points(points: list[PlanPoint], index: int) -> str | None:
    dates = []
    for point in points:
        if point.date not in dates:
            dates.append(point.date)
    return dates[index] if len(dates) > index else None


def _mode_to_command(mode: str) -> str:
    if mode in ("charge_grid", "charge_pv_surplus", "charge_follow", "input"):
        return COMMAND_INPUT
    if mode.startswith("discharge") or mode == "output":
        return COMMAND_OUTPUT
    return COMMAND_IDLE


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

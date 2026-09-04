"""Immutable inputs for one Home Assistant planning refresh."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from .component_config import LoadComponentSpec
from .contracts import HistoricalFeatureSlot, LoadForecastContext, WeatherSlot
from .planning_state import PLANNING_STATE_FILENAME, PlanningStateStore


@dataclass(frozen=True, slots=True)
class PlanningRuntimeSettings:
    """Validated configuration used by one planning refresh."""

    config_dir: str
    timezone: ZoneInfo
    battery_capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    max_charge_power_w: float
    max_discharge_power_w: float
    pv_charging_allowed: bool
    grid_charging_allowed: bool
    discharge_allowed: bool
    discharge_mode: str
    planning_horizon_h: int
    round_trip_efficiency: float
    min_margin_ct_per_kwh: float
    export_opportunity_ct_per_kwh: float
    pv_capacity_kwp: float
    pv_inverter_kw: float

    @property
    def state_file(self) -> str:
        return f"{self.config_dir.rstrip('/')}/{PLANNING_STATE_FILENAME}"

    @property
    def min_energy_kwh(self) -> float:
        return self.battery_capacity_kwh * self.min_soc_pct / 100.0

    @property
    def max_energy_kwh(self) -> float:
        return self.battery_capacity_kwh * self.max_soc_pct / 100.0

    @property
    def max_power_w(self) -> float:
        return max(self.max_charge_power_w, self.max_discharge_power_w)

    @property
    def charge_efficiency(self) -> float:
        return self.round_trip_efficiency**0.5

    @property
    def discharge_efficiency(self) -> float:
        return self.round_trip_efficiency**0.5

    def with_min_soc_pct(self, min_soc_pct: float) -> PlanningRuntimeSettings:
        """Return run-local constraints after reading the battery's live floor."""
        bounded = max(0.0, min(self.max_soc_pct, float(min_soc_pct)))
        return replace(self, min_soc_pct=bounded)


@dataclass(frozen=True, slots=True)
class PlanningRuntime:
    """Complete immutable data snapshot for one planning refresh."""

    captured_at_ms: int
    settings: PlanningRuntimeSettings
    state_store: PlanningStateStore
    states: Mapping[str, Any]
    history_series: Mapping[str, tuple[tuple[float, float], ...]]
    price_intervals: tuple[Mapping[str, Any], ...]
    forecast_history: tuple[HistoricalFeatureSlot, ...]
    forecast_weather: tuple[WeatherSlot, ...]
    forecast_context: LoadForecastContext | None
    forecast_component_specs: tuple[LoadComponentSpec, ...]

    @property
    def captured_at_s(self) -> float:
        """Return the single adapter-captured run time in epoch seconds."""
        return self.captured_at_ms / 1000.0

    @classmethod
    def from_mapping(cls, context: Mapping[str, Any]) -> PlanningRuntime:
        """Validate and freeze the adapter-owned runtime mapping."""
        if "captured_at_ms" not in context:
            raise ValueError("planning runtime requires captured_at_ms")
        captured_at_ms = int(context["captured_at_ms"])
        if captured_at_ms < 0:
            raise ValueError("captured_at_ms must be non-negative")
        capacity = max(0.5, float(context.get("battery_capacity_kwh") or 6.0))
        min_soc = max(0.0, min(100.0, float(context.get("min_soc_pct") or 0.0)))
        max_soc = max(min_soc, min(100.0, float(context.get("max_soc_pct") or 100.0)))
        max_charge = max(
            0.0,
            float(
                context.get("max_charge_power_w")
                or context.get("max_power_w")
                or 2400.0
            ),
        )
        max_discharge = max(
            0.0,
            float(
                context.get("max_discharge_power_w")
                or context.get("max_power_w")
                or 2400.0
            ),
        )
        rte = max(
            0.01,
            min(1.0, float(context.get("round_trip_efficiency") or 0.8)),
        )
        pv_capacity = max(0.1, float(context.get("pv_capacity_kwp") or 1.0))
        discharge_mode = str(context.get("discharge") or "load")
        settings = PlanningRuntimeSettings(
            config_dir=str(context.get("config_dir") or "/config"),
            timezone=ZoneInfo(str(context.get("timezone") or "UTC")),
            battery_capacity_kwh=capacity,
            min_soc_pct=min_soc,
            max_soc_pct=max_soc,
            max_charge_power_w=max_charge,
            max_discharge_power_w=max_discharge,
            pv_charging_allowed=str(context.get("pv_charging") or "on") != "off",
            grid_charging_allowed=str(context.get("grid_charging") or "off") != "off",
            discharge_allowed=discharge_mode != "off",
            discharge_mode=discharge_mode,
            planning_horizon_h=max(
                1, min(48, int(context.get("planning_horizon_h") or 48))
            ),
            round_trip_efficiency=rte,
            min_margin_ct_per_kwh=max(
                0.0, float(context.get("min_margin_ct_per_kwh", 2.0))
            ),
            export_opportunity_ct_per_kwh=max(
                0.0, float(context.get("feed_in_tariff_ct_per_kwh", 0.0))
            ),
            pv_capacity_kwp=pv_capacity,
            pv_inverter_kw=max(
                0.1, float(context.get("pv_inverter_power_kw") or pv_capacity)
            ),
        )
        states = MappingProxyType(
            {
                str(key): _deep_freeze(value)
                for key, value in (context.get("states") or {}).items()
            }
        )
        history = MappingProxyType(
            {
                key: tuple((float(ts), float(value)) for ts, value in values)
                for key, values in (context.get("history_series") or {}).items()
            }
        )
        prices = tuple(
            MappingProxyType(
                {str(key): _deep_freeze(value) for key, value in item.items()}
            )
            for item in (context.get("price_intervals") or ())
        )
        state_store = context.get("state_store")
        if not isinstance(state_store, PlanningStateStore):
            state_store = PlanningStateStore(settings.state_file)
        return cls(
            captured_at_ms=captured_at_ms,
            settings=settings,
            state_store=state_store,
            states=states,
            history_series=history,
            price_intervals=prices,
            forecast_history=tuple(context.get("forecast_history") or ()),
            forecast_weather=tuple(context.get("forecast_weather") or ()),
            forecast_context=context.get("forecast_context"),
            forecast_component_specs=tuple(
                context.get("forecast_component_specs") or ()
            ),
        )


def _deep_freeze(value: Any) -> Any:
    """Detach and recursively freeze mutable values owned by Home Assistant."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value

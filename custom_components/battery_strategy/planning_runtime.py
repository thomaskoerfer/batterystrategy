"""Immutable domain inputs for one Home Assistant planning refresh."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .component_config import LoadComponentSpec
from .contracts import HistoricalFeatureSlot, LoadForecastContext, WeatherSlot
from .runtime_market_data import TariffSchedule

if TYPE_CHECKING:
    from .models import StrategyOptions


class HistoryRole(StrEnum):
    """Domain roles available from normalized Recorder history."""

    PRICE_EUR = "price_eur"
    GRID_IMPORT = "grid_import"
    GRID_EXPORT = "grid_export"
    PV_POWER = "pv_power"
    BATTERY_SOC = "battery_soc"
    BATTERY_INPUT_ENERGY = "battery_input_energy"
    BATTERY_OUTPUT_ENERGY = "battery_output_energy"
    BATTERY_POWER = "battery_power"
    EV_POWER = "ev_power"


@dataclass(frozen=True, slots=True)
class PlanningHistory:
    """Immutable, role-keyed and capture-bounded historical observations."""

    _series: Mapping[HistoryRole, tuple[tuple[float, float], ...]]

    def __post_init__(self) -> None:
        normalized = {
            role if isinstance(role, HistoryRole) else HistoryRole(role): tuple(
                (float(timestamp), float(value)) for timestamp, value in series
            )
            for role, series in self._series.items()
        }
        object.__setattr__(self, "_series", MappingProxyType(normalized))

    @classmethod
    def empty(cls) -> PlanningHistory:
        return cls(MappingProxyType({}))

    @classmethod
    def from_series(
        cls,
        values: Mapping[HistoryRole | str, Iterable[tuple[float, float]]],
        *,
        captured_at_s: float,
    ) -> PlanningHistory:
        normalized = {}
        for raw_role, series in values.items():
            role = (
                raw_role if isinstance(raw_role, HistoryRole) else HistoryRole(raw_role)
            )
            normalized[role] = tuple(
                (float(timestamp), float(value))
                for timestamp, value in series
                if float(timestamp) <= captured_at_s
            )
        return cls(normalized)

    def read(
        self, roles: Iterable[HistoryRole], cutoff_ts: float
    ) -> dict[HistoryRole, list[tuple[float, float]]]:
        cutoff = float(cutoff_ts)
        return {
            role: [item for item in self._series.get(role, ()) if item[0] >= cutoff]
            for role in roles
        }


@dataclass(frozen=True, slots=True)
class PlanningObservations:
    """Normalized current measurements and derived planning observations."""

    current_price_ct_per_kwh: float | None
    future_max_price_ct_per_kwh: float | None
    grid_import_w: float
    grid_export_w: float
    pv_generation_w: float
    battery_charge_w: float
    battery_discharge_w: float
    battery_soc_pct: float | None
    battery_min_soc_pct: float
    ev_charge_w: float
    heat_pump_power_w: float
    pv_next_hour_kwh: float
    pv_tomorrow_kwh: float | None
    cloud_cover_pct: float
    shortwave_radiation_w_m2: float


@dataclass(frozen=True, slots=True)
class PlanningRuntimeSettings:
    """Validated configuration used by one planning refresh."""

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

    @classmethod
    def from_options(
        cls, options: StrategyOptions, timezone: str | ZoneInfo
    ) -> PlanningRuntimeSettings:
        zone = timezone if isinstance(timezone, ZoneInfo) else ZoneInfo(str(timezone))
        capacity = max(0.5, float(options.battery_capacity_kwh or 6.0))
        min_soc = max(0.0, min(100.0, float(options.min_soc_pct or 0.0)))
        max_soc = max(
            min_soc, min(100.0, float(options.max_soc_pct or 100.0))
        )
        fallback_power = max(
            float(options.max_charge_power_w), float(options.max_discharge_power_w)
        ) or 2400.0
        max_charge = max(0.0, float(options.max_charge_power_w or fallback_power))
        max_discharge = max(
            0.0, float(options.max_discharge_power_w or fallback_power)
        )
        rte = max(
            0.01, min(1.0, float(options.round_trip_efficiency or 0.8))
        )
        pv_capacity = max(0.1, float(options.pv_capacity_kwp) or 1.0)
        discharge_mode = str(options.discharge)
        return cls(
            timezone=zone,
            battery_capacity_kwh=capacity,
            min_soc_pct=min_soc,
            max_soc_pct=max_soc,
            max_charge_power_w=max_charge,
            max_discharge_power_w=max_discharge,
            pv_charging_allowed=str(options.pv_charging) != "off",
            grid_charging_allowed=str(options.grid_charging) != "off",
            discharge_allowed=discharge_mode != "off",
            discharge_mode=discharge_mode,
            planning_horizon_h=max(
                1, min(48, int(options.planning_horizon_h or 48))
            ),
            round_trip_efficiency=rte,
            min_margin_ct_per_kwh=max(0.0, float(options.min_margin_ct_per_kwh)),
            export_opportunity_ct_per_kwh=max(
                0.0, float(options.feed_in_tariff_ct_per_kwh)
            ),
            pv_capacity_kwp=pv_capacity,
            pv_inverter_kw=max(
                0.1, float(options.pv_inverter_power_kw) or pv_capacity
            ),
        )

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
    """Complete immutable domain snapshot for one planning refresh."""

    captured_at_ms: int
    settings: PlanningRuntimeSettings
    observations: PlanningObservations
    history: PlanningHistory
    tariffs: TariffSchedule
    forecast_history: tuple[HistoricalFeatureSlot, ...]
    forecast_weather: tuple[WeatherSlot, ...]
    forecast_context: LoadForecastContext | None
    forecast_component_specs: tuple[LoadComponentSpec, ...]

    def __post_init__(self) -> None:
        if self.captured_at_ms < 0:
            raise ValueError("captured_at_ms must be non-negative")
        object.__setattr__(self, "forecast_history", tuple(self.forecast_history))
        object.__setattr__(self, "forecast_weather", tuple(self.forecast_weather))
        object.__setattr__(
            self, "forecast_component_specs", tuple(self.forecast_component_specs)
        )

    @property
    def captured_at_s(self) -> float:
        return self.captured_at_ms / 1000.0

    def with_history(self, history: PlanningHistory) -> PlanningRuntime:
        """Complete the event-loop capture with executor-owned Recorder history."""
        return replace(self, history=history)

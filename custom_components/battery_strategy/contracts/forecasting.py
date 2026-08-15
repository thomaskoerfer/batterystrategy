"""Contracts between feature engineering, forecasting and optimization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .common import (
    DataQuality,
    SlotKey,
    require_finite,
    require_nonnegative,
    require_slots_sorted_unique,
)


@dataclass(frozen=True, slots=True)
class HistoricalFeatureSlot:
    """Finalized actual energy flows for one slot, all in kWh."""

    slot: SlotKey
    house_load_no_ev_kwh: float
    pv_generation_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_charge_kwh: float
    battery_discharge_kwh: float
    ev_charge_kwh: float
    price_ct_per_kwh: float | None
    quality: DataQuality = DataQuality()

    def __post_init__(self) -> None:
        for name in (
            "house_load_no_ev_kwh",
            "pv_generation_kwh",
            "grid_import_kwh",
            "grid_export_kwh",
            "battery_charge_kwh",
            "battery_discharge_kwh",
            "ev_charge_kwh",
        ):
            require_nonnegative(name, getattr(self, name))
        if self.price_ct_per_kwh is not None:
            require_finite("price_ct_per_kwh", self.price_ct_per_kwh)


@dataclass(frozen=True, slots=True)
class WeatherSlot:
    """Exogenous weather features aligned to a planning slot."""

    slot: SlotKey
    shortwave_radiation_w_m2: float | None = None
    cloud_cover_pct: float | None = None
    temperature_c: float | None = None
    quality: DataQuality = DataQuality()

    def __post_init__(self) -> None:
        if self.shortwave_radiation_w_m2 is not None:
            require_nonnegative(
                "shortwave_radiation_w_m2", self.shortwave_radiation_w_m2
            )
        if self.cloud_cover_pct is not None:
            if not 0.0 <= float(self.cloud_cover_pct) <= 100.0:
                raise ValueError("cloud_cover_pct must be between 0 and 100")
        if self.temperature_c is not None:
            require_finite("temperature_c", self.temperature_c)


@dataclass(frozen=True, slots=True)
class ForecastRequest:
    """Explicit forecast time grid; forecasters must not read the wall clock."""

    as_of_ms: int
    timezone: str
    slots: tuple[SlotKey, ...]

    def __post_init__(self) -> None:
        if self.as_of_ms < 0 or not self.timezone:
            raise ValueError("forecast request requires as_of_ms and timezone")
        require_slots_sorted_unique(self.slots)


@dataclass(frozen=True, slots=True)
class QuantileEnergy:
    """P10/P50/P90 slot energy in kWh."""

    p10_kwh: float
    p50_kwh: float
    p90_kwh: float

    def __post_init__(self) -> None:
        for name in ("p10_kwh", "p50_kwh", "p90_kwh"):
            require_nonnegative(name, getattr(self, name))
        if not self.p10_kwh <= self.p50_kwh <= self.p90_kwh:
            raise ValueError("forecast quantiles must satisfy p10 <= p50 <= p90")


@dataclass(frozen=True, slots=True)
class ForecastSlot:
    """One forecast quantity for one planning slot."""

    slot: SlotKey
    energy: QuantileEnergy
    quality: DataQuality = DataQuality()


@dataclass(frozen=True, slots=True)
class LoadForecast:
    """House-load forecast excluding EV consumption."""

    forecast_id: str
    generated_at_ms: int
    training_cutoff_ms: int
    model_version: str
    slots: tuple[ForecastSlot, ...]

    def __post_init__(self) -> None:
        _validate_forecast_series(self)


@dataclass(frozen=True, slots=True)
class PvForecast:
    """PV-generation forecast before battery or grid decisions."""

    forecast_id: str
    generated_at_ms: int
    training_cutoff_ms: int
    model_version: str
    slots: tuple[ForecastSlot, ...]

    def __post_init__(self) -> None:
        _validate_forecast_series(self)


@dataclass(frozen=True, slots=True)
class ForecastBundle:
    """Aligned load and PV forecasts consumed by optimization."""

    load: LoadForecast
    pv: PvForecast

    def __post_init__(self) -> None:
        if tuple(item.slot for item in self.load.slots) != tuple(
            item.slot for item in self.pv.slots
        ):
            raise ValueError("load and PV forecasts must use the same slot grid")


@dataclass(frozen=True, slots=True)
class PvPlant:
    """PV capacity visible to the forecaster at the requested horizon."""

    generator_kwp: float
    inverter_kw: float

    def __post_init__(self) -> None:
        require_nonnegative("generator_kwp", self.generator_kwp)
        require_nonnegative("inverter_kw", self.inverter_kw)


class LoadForecaster(Protocol):
    """Pure load-forecast boundary."""

    def forecast(
        self,
        request: ForecastRequest,
        history: tuple[HistoricalFeatureSlot, ...],
    ) -> LoadForecast: ...


class PvForecaster(Protocol):
    """Pure PV-forecast boundary."""

    def forecast(
        self,
        request: ForecastRequest,
        history: tuple[HistoricalFeatureSlot, ...],
        weather: tuple[WeatherSlot, ...],
        plant: PvPlant,
    ) -> PvForecast: ...


def _validate_forecast_series(series: LoadForecast | PvForecast) -> None:
    if not series.forecast_id or not series.model_version:
        raise ValueError("forecast_id and model_version are required")
    if series.generated_at_ms < 0 or series.training_cutoff_ms < 0:
        raise ValueError("forecast timestamps must be non-negative")
    if series.training_cutoff_ms > series.generated_at_ms:
        raise ValueError("training_cutoff_ms cannot exceed generated_at_ms")
    require_slots_sorted_unique(tuple(item.slot for item in series.slots))

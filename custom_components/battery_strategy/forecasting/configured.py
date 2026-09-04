"""Configured production implementations of the forecast contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

from ..component_config import LoadComponentSpec
from ..contracts import (
    ForecastBundle,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadForecast,
    LoadForecastContext,
    LoadForecaster,
    PvForecast,
    PvForecaster,
    PvPlant,
    WeatherSlot,
)
from .components import build_component_load_forecast
from .feature_store import eligible_feature_history, feature_samples
from .history import ForecastTargetInput
from .load import LoadForecastModelConfig, build_load_forecast
from .pv import PvForecastModelConfig, build_pv_forecast


@dataclass(frozen=True, slots=True)
class ConfiguredLoadForecaster:
    """Production load owner with only load-specific configuration."""

    config: LoadForecastModelConfig
    default_weather_factor: float
    component_specs: tuple[LoadComponentSpec, ...] = ()

    def forecast(
        self,
        request: ForecastRequest,
        history: tuple[HistoricalFeatureSlot, ...],
        context: LoadForecastContext,
        weather: tuple[WeatherSlot, ...],
    ) -> LoadForecast:
        """Forecast EV-free load without invoking PV logic."""
        eligible = eligible_feature_history(history, request.as_of_ms)
        targets = weather_targets(request, weather, self.default_weather_factor)
        if self.component_specs:
            return build_component_load_forecast(
                request,
                eligible,
                targets,
                context,
                weather,
                self.component_specs,
                self.config,
            )
        return build_load_forecast(
            request,
            feature_samples(eligible),
            targets,
            context,
            self.config,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredPvForecaster:
    """Production PV owner with only PV-specific configuration."""

    config: PvForecastModelConfig
    default_weather_factor: float

    def forecast(
        self,
        request: ForecastRequest,
        history: tuple[HistoricalFeatureSlot, ...],
        weather: tuple[WeatherSlot, ...],
        plant: PvPlant,
    ) -> PvForecast:
        """Forecast PV from explicit weather and plant contract inputs."""
        eligible = eligible_feature_history(history, request.as_of_ms)
        return build_pv_forecast(
            request,
            feature_samples(eligible),
            weather_targets(request, weather, self.default_weather_factor),
            replace(
                self.config,
                pv_capacity_kwp=plant.generator_kwp,
                pv_inverter_kw=plant.inverter_kw,
            ),
        )


@dataclass(frozen=True, slots=True)
class ForecastComposer:
    """Compose independent contract implementations into one aligned bundle."""

    load_forecaster: LoadForecaster
    pv_forecaster: PvForecaster

    def compose(
        self,
        request: ForecastRequest,
        history: tuple[HistoricalFeatureSlot, ...],
        context: LoadForecastContext,
        weather: tuple[WeatherSlot, ...],
        plant: PvPlant,
    ) -> ForecastBundle:
        """Combine forecasts without adding forecasting policy."""
        return ForecastBundle(
            load=self.load_forecaster.forecast(request, history, context, weather),
            pv=self.pv_forecaster.forecast(request, history, weather, plant),
        )


def weather_targets(
    request: ForecastRequest,
    weather: tuple[WeatherSlot, ...],
    default_weather_factor: float,
) -> tuple[ForecastTargetInput, ...]:
    """Build the established hourly weather target grid deterministically."""
    timezone = ZoneInfo(request.timezone)
    by_hour: dict[str, float] = {}
    for item in weather:
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, dt.timezone.utc
        ).astimezone(timezone)
        key = local.replace(minute=0, second=0, microsecond=0).isoformat()
        # Preserve the old hourly dictionary behavior: the last quarter wins.
        by_hour[key] = _weather_factor(
            item.cloud_cover_pct,
            item.shortwave_radiation_w_m2,
        )

    result = []
    for slot in request.slots:
        local = dt.datetime.fromtimestamp(
            slot.start_ms / 1000.0, dt.timezone.utc
        ).astimezone(timezone)
        key = local.replace(minute=0, second=0, microsecond=0).isoformat()
        result.append(
            ForecastTargetInput(
                local,
                by_hour.get(key, float(default_weather_factor)),
            )
        )
    return tuple(result)


def _weather_factor(cloud_cover: float | None, radiation: float | None) -> float:
    cloud_factor = max(0.35, min(1.05, 1.0 - float(cloud_cover or 0.0) / 130.0))
    radiation_factor = max(0.2, min(1.1, float(radiation or 0.0) / 650.0))
    return 0.6 * cloud_factor + 0.4 * radiation_factor

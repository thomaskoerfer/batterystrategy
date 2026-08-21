"""Compatibility facade composing independently owned load and PV forecasts."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ForecastBundle, ForecastRequest, LoadForecastContext
from .history import LegacyForecastSample, LegacyForecastTarget
from .load import LegacyLoadForecastConfig, build_legacy_load_forecast
from .pv import LegacyPvForecastConfig, build_legacy_pv_forecast


@dataclass(frozen=True, slots=True)
class LegacyForecastConfig:
    """Transitional aggregate config kept for production-call compatibility."""

    timezone: str
    load_bias: float
    load_slot_biases: tuple[float, ...]
    pv_global_bias: float
    pv_slot_biases: tuple[float, ...]
    current_weather_factor: float
    current_pv_w: float | None
    tomorrow_date: str
    tomorrow_energy_kwh: float | None
    capacity_events: tuple[tuple[str, float, float], ...]

    def load_config(self) -> LegacyLoadForecastConfig:
        """Return only state owned by the load model."""
        return LegacyLoadForecastConfig(
            self.timezone, self.load_bias, self.load_slot_biases
        )

    def pv_config(self) -> LegacyPvForecastConfig:
        """Return only state owned by the PV model."""
        return LegacyPvForecastConfig(
            self.timezone,
            self.pv_global_bias,
            self.pv_slot_biases,
            self.current_weather_factor,
            self.current_pv_w,
            self.tomorrow_date,
            self.tomorrow_energy_kwh,
            self.capacity_events,
        )


def build_legacy_forecast(
    request: ForecastRequest,
    samples: tuple[LegacyForecastSample, ...],
    targets: tuple[LegacyForecastTarget, ...],
    load_context: LoadForecastContext,
    config: LegacyForecastConfig,
) -> ForecastBundle:
    """Compose independent load and PV results for the optimizer boundary."""
    return ForecastBundle(
        load=build_legacy_load_forecast(
            request, samples, targets, load_context, config.load_config()
        ),
        pv=build_legacy_pv_forecast(request, samples, targets, config.pv_config()),
    )

"""Configuration facade composing independently owned load and PV forecasts."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ForecastBundle, ForecastRequest, LoadForecastContext
from .history import ForecastHistorySample, ForecastTargetInput
from .load import LoadForecastModelConfig, build_load_forecast
from .pv import PvForecastModelConfig, build_pv_forecast


@dataclass(frozen=True, slots=True)
class ForecastModelConfig:
    """Shared immutable configuration split into load- and PV-owned subsets."""

    timezone: str
    load_bias: float
    load_slot_biases: tuple[float, ...]
    pv_global_bias: float
    pv_slot_biases: tuple[float, ...]
    current_weather_factor: float
    current_pv_w: float | None
    tomorrow_date: str
    tomorrow_energy_kwh: float | None
    pv_capacity_kwp: float
    pv_inverter_kw: float

    def load_config(self) -> LoadForecastModelConfig:
        """Return only state owned by the load model."""
        return LoadForecastModelConfig(
            self.timezone, self.load_bias, self.load_slot_biases
        )

    def pv_config(self) -> PvForecastModelConfig:
        """Return only state owned by the PV model."""
        return PvForecastModelConfig(
            self.timezone,
            self.pv_global_bias,
            self.pv_slot_biases,
            self.current_weather_factor,
            self.current_pv_w,
            self.tomorrow_date,
            self.tomorrow_energy_kwh,
            self.pv_capacity_kwp,
            self.pv_inverter_kw,
        )


def build_forecast_bundle(
    request: ForecastRequest,
    samples: tuple[ForecastHistorySample, ...],
    targets: tuple[ForecastTargetInput, ...],
    load_context: LoadForecastContext,
    config: ForecastModelConfig,
) -> ForecastBundle:
    """Compose independent load and PV results for the optimizer boundary."""
    return ForecastBundle(
        load=build_load_forecast(
            request, samples, targets, load_context, config.load_config()
        ),
        pv=build_pv_forecast(request, samples, targets, config.pv_config()),
    )

"""Production forecasting from finalized recorder-independent features."""

from __future__ import annotations

from dataclasses import dataclass

from ..component_config import LoadComponentSpec
from ..contracts import (
    ForecastBundle,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadForecast,
    LoadForecastContext,
    PvForecast,
    QualityFlag,
    WeatherSlot,
)
from .baseline import ForecastModelConfig
from .components import build_component_load_forecast
from .history import ForecastHistorySample, ForecastTargetInput
from .load import build_load_forecast
from .pv import build_pv_forecast

SLOT_H = 0.25
MIN_PRODUCTION_HISTORY_SLOTS = 7 * 96
_LOAD_INVALID_FLAGS = frozenset(
    {
        QualityFlag.MISSING_GRID,
        QualityFlag.MISSING_PV,
        QualityFlag.MISSING_BATTERY,
        QualityFlag.MISSING_EV,
        QualityFlag.RESTART_GAP,
    }
)
_PV_INVALID_FLAGS = frozenset({QualityFlag.MISSING_PV, QualityFlag.RESTART_GAP})


class FeatureStoreForecastNotReady(RuntimeError):
    """Raised when the production feature history has not passed its gate."""


@dataclass(frozen=True, slots=True)
class FeatureStoreForecastReadiness:
    """Readiness of one immutable feature snapshot for production use."""

    history_slots: int
    load_usable_slots: int
    pv_usable_slots: int
    component_usable_slots: int
    history_span_days: float
    ready: bool
    reason: str | None


def build_feature_store_forecast(
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    targets: tuple[ForecastTargetInput, ...],
    context: LoadForecastContext,
    config: ForecastModelConfig,
    *,
    weather: tuple[WeatherSlot, ...] = (),
    component_specs: tuple[LoadComponentSpec, ...] = (),
    require_ready: bool = True,
) -> ForecastBundle:
    """Build the sole production forecast from finalized feature slots."""
    eligible = eligible_feature_history(history, request.as_of_ms)
    readiness = feature_store_forecast_readiness(
        eligible, component_specs=component_specs
    )
    if require_ready and not readiness.ready:
        raise FeatureStoreForecastNotReady(readiness.reason or "not_ready")
    samples = feature_samples(eligible)
    return ForecastBundle(
        load=build_feature_store_load_forecast(
            request,
            eligible,
            samples,
            targets,
            context,
            config,
            weather=weather,
            component_specs=component_specs,
        ),
        pv=build_feature_store_pv_forecast(request, samples, targets, config),
    )


def build_feature_store_load_forecast(
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    samples: tuple[ForecastHistorySample, ...],
    targets: tuple[ForecastTargetInput, ...],
    context: LoadForecastContext,
    config: ForecastModelConfig,
    *,
    weather: tuple[WeatherSlot, ...] = (),
    component_specs: tuple[LoadComponentSpec, ...] = (),
) -> LoadForecast:
    """Build EV-free load independently from PV configuration and logic."""
    if component_specs:
        return build_component_load_forecast(
            request,
            history,
            targets,
            context,
            weather,
            component_specs,
            config.load_config(),
        )
    return build_load_forecast(request, samples, targets, context, config.load_config())


def build_feature_store_pv_forecast(
    request: ForecastRequest,
    samples: tuple[ForecastHistorySample, ...],
    targets: tuple[ForecastTargetInput, ...],
    config: ForecastModelConfig,
) -> PvForecast:
    """Build PV independently from load components and load context."""
    return build_pv_forecast(request, samples, targets, config.pv_config())


def eligible_feature_history(
    history: tuple[HistoricalFeatureSlot, ...], as_of_ms: int
) -> tuple[HistoricalFeatureSlot, ...]:
    """Return finalized history available at generation time."""
    return tuple(item for item in history if item.slot.end_ms <= as_of_ms)


def feature_samples(
    history: tuple[HistoricalFeatureSlot, ...],
) -> tuple[ForecastHistorySample, ...]:
    """Adapt canonical slot energy to the pure slot-profile mathematics."""
    samples = []
    for item in history[-6000:]:
        flags = frozenset(item.quality.flags)
        samples.append(
            ForecastHistorySample(
                ts_s=item.slot.start_ms / 1000.0,
                load_w=item.house_load_no_ev_kwh / SLOT_H * 1000.0,
                pv_w=item.pv_generation_kwh / SLOT_H * 1000.0,
                grid_import_w=item.grid_import_kwh / SLOT_H * 1000.0,
                grid_export_w=item.grid_export_kwh / SLOT_H * 1000.0,
                load_valid=(
                    item.quality.coverage >= 0.999 and not flags & _LOAD_INVALID_FLAGS
                ),
                pv_valid=(
                    item.quality.coverage >= 0.999 and not flags & _PV_INVALID_FLAGS
                ),
            )
        )
    return tuple(samples)


def feature_store_forecast_readiness(
    history: tuple[HistoricalFeatureSlot, ...],
    *,
    component_specs: tuple[LoadComponentSpec, ...] = (),
) -> FeatureStoreForecastReadiness:
    """Evaluate the explicit production gate without reading external state."""
    samples = feature_samples(history)
    load_usable = sum(item.load_valid for item in samples)
    pv_usable = sum(item.pv_valid for item in samples)
    span_days = (
        (history[-1].slot.end_ms - history[0].slot.start_ms) / 86_400_000.0
        if history
        else 0.0
    )
    component_keys = tuple(item.component_key for item in component_specs)
    component_usable = (
        sum(_has_usable_components(item, component_keys) for item in history)
        if component_keys
        else MIN_PRODUCTION_HISTORY_SLOTS
    )
    failures = []
    if load_usable < MIN_PRODUCTION_HISTORY_SLOTS:
        failures.append("insufficient_load_history")
    if pv_usable < MIN_PRODUCTION_HISTORY_SLOTS:
        failures.append("insufficient_pv_history")
    if span_days < 7.0:
        failures.append("insufficient_history_span")
    if component_keys and component_usable < MIN_PRODUCTION_HISTORY_SLOTS:
        failures.append("insufficient_component_history")
    return FeatureStoreForecastReadiness(
        len(history),
        load_usable,
        pv_usable,
        component_usable,
        round(span_days, 3),
        not failures,
        ";".join(failures) or None,
    )


def _has_usable_components(item: HistoricalFeatureSlot, keys: tuple[str, ...]) -> bool:
    components = {
        component.component_key: component for component in item.load_components
    }
    return item.quality.coverage >= 0.999 and all(
        key in components and components[key].quality.coverage >= 0.999 for key in keys
    )

"""Pure forecast implementations and transitional migration adapters."""

from .legacy import (
    LegacyForecastConfig,
    LegacyForecastSample,
    LegacyForecastTarget,
    build_legacy_forecast,
)
from .load import LegacyLoadForecastConfig, build_legacy_load_forecast
from .pv import LegacyPvForecastConfig, build_legacy_pv_forecast
from .shadow import evaluate_feature_store_shadow

__all__ = [
    "LegacyForecastConfig",
    "LegacyForecastSample",
    "LegacyForecastTarget",
    "LegacyLoadForecastConfig",
    "LegacyPvForecastConfig",
    "build_legacy_forecast",
    "build_legacy_load_forecast",
    "build_legacy_pv_forecast",
    "evaluate_feature_store_shadow",
]

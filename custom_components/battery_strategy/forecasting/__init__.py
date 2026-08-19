"""Pure forecast implementations and transitional migration adapters."""

from .legacy import (
    LegacyForecastConfig,
    LegacyForecastSample,
    LegacyForecastTarget,
    build_legacy_forecast,
)

__all__ = [
    "LegacyForecastConfig",
    "LegacyForecastSample",
    "LegacyForecastTarget",
    "build_legacy_forecast",
]

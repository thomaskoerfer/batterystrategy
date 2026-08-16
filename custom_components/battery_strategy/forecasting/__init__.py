"""Pure forecast implementations and transitional migration adapters."""

from .legacy_shadow import (
    LegacyForecastConfig,
    LegacyForecastSample,
    LegacyForecastTarget,
    build_legacy_shadow_forecast,
)

__all__ = [
    "LegacyForecastConfig",
    "LegacyForecastSample",
    "LegacyForecastTarget",
    "build_legacy_shadow_forecast",
]

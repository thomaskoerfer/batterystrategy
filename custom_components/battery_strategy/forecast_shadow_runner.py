"""Isolated orchestration for recorder-independent forecast evaluation."""

from __future__ import annotations

import logging
from pathlib import Path

from .component_config import LoadComponentSpec
from .contracts import HistoricalFeatureSlot, LoadDriverSnapshot, WeatherSlot
from .forecast_shadow_store import ForecastShadowTraceStore
from .forecasting.shadow import evaluate_feature_store_shadow_snapshot

LOGGER = logging.getLogger(__name__)


class ForecastShadowRunner:
    """Run and persist shadow forecasts without entering optimization."""

    def __init__(self, path: str | Path, history=()) -> None:
        self._history: tuple[HistoricalFeatureSlot, ...] = tuple(history)
        self._weather: tuple[WeatherSlot, ...] = ()
        self._drivers: tuple[LoadDriverSnapshot, ...] = ()
        self._component_specs: tuple[LoadComponentSpec, ...] = ()
        self._store = ForecastShadowTraceStore(path)

    def set_history(self, history) -> None:
        """Replace the immutable feature-store snapshot used by the next run."""
        self._history = tuple(history)

    def set_environment(self, weather=(), drivers=(), component_specs=()) -> None:
        """Replace immutable I/O snapshots consumed by the next shadow run."""
        self._weather = tuple(weather)
        self._drivers = tuple(drivers)
        self._component_specs = tuple(component_specs)

    def evaluate(self, snapshot: dict[str, object]) -> dict[str, object]:
        """Evaluate one snapshot; failures remain diagnostics only."""
        try:
            run = evaluate_feature_store_shadow_snapshot(
                snapshot,
                self._history,
                weather=self._weather,
                drivers=self._drivers,
                component_specs=self._component_specs,
            )
            return self._store.record(run, self._history)
        except Exception as err:  # noqa: BLE001 - observation must not affect control.
            LOGGER.warning("Forecast shadow evaluation failed: %s", err)
            return {
                "authoritative": False,
                "status": "error",
                "reason": f"{type(err).__name__}: {err}",
            }

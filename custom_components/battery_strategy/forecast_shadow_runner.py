"""Isolated orchestration for recorder-independent forecast evaluation."""

from __future__ import annotations

import logging
from pathlib import Path

from .contracts import HistoricalFeatureSlot
from .forecast_shadow_store import ForecastShadowTraceStore
from .forecasting.shadow import evaluate_feature_store_shadow_snapshot

LOGGER = logging.getLogger(__name__)


class ForecastShadowRunner:
    """Run and persist shadow forecasts without entering optimization."""

    def __init__(self, path: str | Path, history=()) -> None:
        self._history: tuple[HistoricalFeatureSlot, ...] = tuple(history)
        self._store = ForecastShadowTraceStore(path)

    def set_history(self, history) -> None:
        """Replace the immutable feature-store snapshot used by the next run."""
        self._history = tuple(history)

    def evaluate(self, snapshot: dict[str, object]) -> dict[str, object]:
        """Evaluate one snapshot; failures remain diagnostics only."""
        try:
            run = evaluate_feature_store_shadow_snapshot(snapshot, self._history)
            return self._store.record(run, self._history)
        except Exception as err:  # noqa: BLE001 - observation must not affect control.
            LOGGER.warning("Forecast shadow evaluation failed: %s", err)
            return {
                "authoritative": False,
                "status": "error",
                "reason": f"{type(err).__name__}: {err}",
            }

"""Infrastructure ports owned by the data-adapter layer."""

from __future__ import annotations

from typing import Protocol

from .forecasting import ForecastRequest, HistoricalFeatureSlot, WeatherSlot
from .optimization import MarketSlot


class FeatureStore(Protocol):
    """Versioned recorder-independent persistence for finalized features."""

    async def load(
        self, start_ms: int, end_ms: int
    ) -> tuple[HistoricalFeatureSlot, ...]: ...

    async def upsert(self, slots: tuple[HistoricalFeatureSlot, ...]) -> None: ...


class WeatherDataProvider(Protocol):
    """External weather adapter; forecasting receives normalized slots only."""

    async def load(self, request: ForecastRequest) -> tuple[WeatherSlot, ...]: ...


class MarketDataProvider(Protocol):
    """External price adapter; optimization receives normalized slots only."""

    async def load(self, request: ForecastRequest) -> tuple[MarketSlot, ...]: ...

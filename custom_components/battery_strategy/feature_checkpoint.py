"""Home Assistant persistence for the incomplete feature slot."""

from __future__ import annotations

from homeassistant.helpers.storage import Store

from .const import DOMAIN

CHECKPOINT_STORE_VERSION = 1
CHECKPOINT_SAVE_DELAY_S = 300


class FeatureAggregatorCheckpointStore:
    """Persist only private aggregation state; finalized facts stay elsewhere."""

    def __init__(self, hass, entry_id: str) -> None:
        self._store = Store(
            hass,
            CHECKPOINT_STORE_VERSION,
            f"{DOMAIN}.feature_aggregator.{entry_id}",
        )

    async def load(self) -> dict | None:
        """Load the last incomplete-slot checkpoint."""
        payload = await self._store.async_load()
        return payload if isinstance(payload, dict) else None

    def schedule(self, data_func) -> None:
        """Coalesce routine writes while keeping crash loss bounded."""
        self._store.async_delay_save(data_func, CHECKPOINT_SAVE_DELAY_S)

    async def save(self, payload: dict | None) -> None:
        """Persist an exact hand-off point before a graceful unload."""
        if payload is not None:
            await self._store.async_save(payload)

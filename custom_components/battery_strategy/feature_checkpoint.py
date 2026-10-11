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
        self._save_scheduled = False

    async def load(self) -> dict | None:
        """Load the last incomplete-slot checkpoint."""
        payload = await self._store.async_load()
        return payload if isinstance(payload, dict) else None

    def schedule(self, data_func) -> None:
        """Coalesce routine writes while keeping crash loss bounded."""
        if self._save_scheduled:
            return
        self._save_scheduled = True

        def _latest_payload():
            self._save_scheduled = False
            return data_func()

        self._store.async_delay_save(_latest_payload, CHECKPOINT_SAVE_DELAY_S)

    async def save(self, payload: dict | None) -> None:
        """Persist an exact hand-off point before a graceful unload."""
        if payload is not None:
            await self._store.async_save(payload)
        self._save_scheduled = False

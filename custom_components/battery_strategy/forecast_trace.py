"""Bounded, non-authoritative forecast-vintage persistence."""

from __future__ import annotations

import datetime as dt
import fcntl
import gzip
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .contracts import ForecastBundle, ForecastSlot

FORECAST_TRACE_DIRECTORY = "battery_strategy_forecast_trace"
FORECAST_TRACE_SCHEMA_VERSION = 1
FORECAST_TRACE_RETENTION_DAYS = 21
FORECAST_TRACE_MAX_SLOTS = 192
FORECAST_TRACE_MAX_COMPONENTS = 32
SLOT_MS = 15 * 60 * 1000
LOGGER = logging.getLogger(__name__)


class ForecastTraceScheduler:
    """Config-entry-owned scheduler coordinated across reloads by a file lock."""

    def __init__(self, hass, root: str | Path) -> None:
        self._hass = hass
        self._root = Path(root)
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._scheduled_buckets: set[int] = set()
        self._last_warning_monotonic = float("-inf")

    def schedule(
        self,
        entry,
        bundle: ForecastBundle,
        is_active: Callable[[], bool] | None = None,
    ) -> None:
        """Schedule one lifecycle-owned trace without blocking the caller."""
        if is_active is not None and not is_active():
            return
        bucket_ms = forecast_trace_bucket_ms(bundle)
        with self._state_lock:
            if bucket_ms in self._scheduled_buckets:
                return
            self._scheduled_buckets.add(bucket_ms)
            if len(self._scheduled_buckets) > 8:
                self._scheduled_buckets.remove(min(self._scheduled_buckets))

        def schedule_on_loop() -> None:
            if is_active is not None and not is_active():
                self._forget(bucket_ms)
                return
            coroutine = self._async_write(bundle, bucket_ms)
            try:
                entry.async_create_background_task(
                    self._hass,
                    coroutine,
                    "battery_strategy_forecast_trace",
                )
            except Exception as err:
                coroutine.close()
                self._forget(bucket_ms)
                self._warn(err)

        try:
            self._hass.loop.call_soon_threadsafe(schedule_on_loop)
        except Exception as err:
            self._forget(bucket_ms)
            self._warn(err)

    async def _async_write(self, bundle: ForecastBundle, bucket_ms: int) -> None:
        try:
            await self._hass.async_add_executor_job(
                self._write_if_available, bundle, bucket_ms
            )
        except Exception as err:
            self._forget(bucket_ms)
            self._warn(err)

    def _write_if_available(self, bundle: ForecastBundle, bucket_ms: int) -> None:
        # A writer that survived reload owns this lock until its real file I/O
        # returns. Later vintages are dropped instead of waiting in the executor.
        if not self._write_lock.acquire(blocking=False):
            return
        try:
            append_forecast_trace(self._root, bundle)
        except Exception as err:
            self._forget(bucket_ms)
            self._warn(err)
        finally:
            self._write_lock.release()

    def _forget(self, bucket_ms: int) -> None:
        with self._state_lock:
            self._scheduled_buckets.discard(bucket_ms)

    def _warn(self, err: Exception) -> None:
        with self._state_lock:
            now = time.monotonic()
            if now - self._last_warning_monotonic < 3600.0:
                return
            self._last_warning_monotonic = now
        LOGGER.warning("Forecast trace write failed: %s", err)


def append_forecast_trace(root: Path, bundle: ForecastBundle) -> Path | None:
    """Persist one vintage while dropping work behind a surviving writer."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".write.lock"
    with lock_path.open("a+b") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        try:
            return _append_forecast_trace_locked(root, bundle)
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def _append_forecast_trace_locked(root: Path, bundle: ForecastBundle) -> Path | None:
    """Persist at most one immutable forecast vintage per UTC quarter-hour."""
    generated_at_ms = max(bundle.load.generated_at_ms, bundle.pv.generated_at_ms)
    bucket_ms = forecast_trace_bucket_ms(bundle)
    generated_at = dt.datetime.fromtimestamp(generated_at_ms / 1000.0, dt.UTC)
    day_dir = root / generated_at.date().isoformat()
    target = day_dir / f"{bucket_ms}.json.gz"
    if target.exists():
        return None

    load_slots = bundle.load.slots[:FORECAST_TRACE_MAX_SLOTS]
    pv_slots = bundle.pv.slots[:FORECAST_TRACE_MAX_SLOTS]
    component_limit = bundle.load.components[:FORECAST_TRACE_MAX_COMPONENTS]
    payload = {
        "schema_version": FORECAST_TRACE_SCHEMA_VERSION,
        "non_authoritative": True,
        "generated_at_ms": generated_at_ms,
        "vintage_bucket_ms": bucket_ms,
        "load": {
            "forecast_id": bundle.load.forecast_id,
            "generated_at_ms": bundle.load.generated_at_ms,
            "model_version": bundle.load.model_version,
            "training_cutoff_ms": bundle.load.training_cutoff_ms,
            "slots": [_serialize_slot(item) for item in load_slots],
            "components": [
                {
                    "component_key": component.component_key,
                    "model_version": component.model_version,
                    "training_cutoff_ms": component.training_cutoff_ms,
                    "slots": [
                        _serialize_slot(item)
                        for item in component.slots[:FORECAST_TRACE_MAX_SLOTS]
                    ],
                }
                for component in component_limit
            ],
        },
        "pv": {
            "forecast_id": bundle.pv.forecast_id,
            "generated_at_ms": bundle.pv.generated_at_ms,
            "model_version": bundle.pv.model_version,
            "training_cutoff_ms": bundle.pv.training_cutoff_ms,
            "slots": [_serialize_slot(item) for item in pv_slots],
        },
        "truncated": bool(
            len(bundle.load.slots) > FORECAST_TRACE_MAX_SLOTS
            or len(bundle.pv.slots) > FORECAST_TRACE_MAX_SLOTS
            or len(bundle.load.components) > FORECAST_TRACE_MAX_COMPONENTS
        ),
    }
    day_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        try:
            # A hard link publishes the complete file atomically and preserves
            # the first vintage if a config-entry reload races this writer.
            os.link(temporary, target)
        except FileExistsError:
            return None
    finally:
        temporary.unlink(missing_ok=True)
    _remove_expired_days(root, generated_at.date())
    return target


def forecast_trace_bucket_ms(bundle: ForecastBundle) -> int:
    """Return the shared UTC vintage bucket used for scheduling and storage."""
    return (
        max(bundle.load.generated_at_ms, bundle.pv.generated_at_ms) // SLOT_MS * SLOT_MS
    )


def _serialize_slot(item: ForecastSlot) -> list[object]:
    return [
        item.slot.start_ms,
        item.slot.end_ms,
        item.energy.p50_kwh,
        item.energy.p10_kwh,
        item.energy.p90_kwh,
        item.energy.calibration_samples,
        item.quality.coverage,
        [flag.value for flag in item.quality.flags],
    ]


def _remove_expired_days(root: Path, current_day: dt.date) -> None:
    cutoff = current_day - dt.timedelta(days=FORECAST_TRACE_RETENTION_DAYS - 1)
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            day = dt.date.fromisoformat(path.name)
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(path)

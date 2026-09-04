"""Non-blocking optimizer plan lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .contracts import LiveMeasurements
from .models import StrategyOptions
from .planning_adapter import PlanningCapture, PlanningPipelineAdapter
from .planning_result import PlanningResult

LOGGER = logging.getLogger(__name__)


class BackgroundPlanner:
    """Keep one optimizer run in flight while serving the last valid plan."""

    def __init__(
        self,
        hass,
        adapter: PlanningPipelineAdapter,
        on_complete: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._adapter = adapter
        self._task: asyncio.Future | None = None
        self._closing = False
        self._pending_force = False
        self._last_error: str | None = None
        self._on_complete = on_complete

    def current(
        self,
        inputs: LiveMeasurements,
        options: StrategyOptions,
    ) -> PlanningResult:
        """Return the latest valid plan re-evaluated against current inputs."""
        return self._adapter.cached_result(inputs, options)

    def maybe_schedule(
        self,
        inputs: LiveMeasurements,
        options: StrategyOptions,
        runtime_context: PlanningCapture,
        *,
        force: bool = False,
    ) -> bool:
        """Schedule one optimizer refresh without blocking the live loop."""
        self._collect_finished()
        if self._closing:
            return False
        if self._task is not None:
            self._pending_force = self._pending_force or force
            return False
        effective_force = force or self._pending_force
        if not self._adapter.needs_run(options, force=effective_force):
            return False
        self._pending_force = False
        self._task = self._hass.async_add_executor_job(
            self._adapter.run,
            inputs,
            options,
            True,
            runtime_context,
        )
        if self._on_complete is not None:
            self._task.add_done_callback(self._request_completion_refresh)
        return True

    def _request_completion_refresh(self, _task: asyncio.Future) -> None:
        """Publish a completed background result without waiting for polling."""
        if not self._closing and self._on_complete is not None:
            self._hass.async_create_task(self._on_complete())

    def _collect_finished(self) -> None:
        if self._task is None or not self._task.done():
            return
        task = self._task
        self._task = None
        try:
            task.result()
            self._last_error = None
        except asyncio.CancelledError:
            pass
        except Exception as err:  # pragma: no cover - HA logs executor failures.
            self._last_error = f"{type(err).__name__}: {err}"
            LOGGER.exception("Battery Strategy optimizer refresh failed")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> str | None:
        self._collect_finished()
        return self._last_error

    def begin_shutdown(self) -> None:
        """Synchronously suppress completion callbacks before unload awaits."""
        self._closing = True

    def abort_shutdown(self) -> None:
        """Resume normal scheduling when Home Assistant rejects an unload."""
        self._closing = False

    async def async_shutdown(self) -> None:
        """Stop accepting results during integration unload."""
        self.begin_shutdown()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

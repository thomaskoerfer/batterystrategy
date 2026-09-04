"""Non-blocking optimizer plan lifecycle."""

from __future__ import annotations

import asyncio
import logging

from .models import StrategyInputs, StrategyOptions
from .planning_adapter import PlanningPipelineAdapter
from .planning_result import PlanningResult

LOGGER = logging.getLogger(__name__)


class BackgroundPlanner:
    """Keep one optimizer run in flight while serving the last valid plan."""

    def __init__(self, hass, adapter: PlanningPipelineAdapter) -> None:
        self._hass = hass
        self._adapter = adapter
        self._task: asyncio.Future | None = None
        self._closing = False
        self._pending_force = False
        self._last_error: str | None = None

    def current(
        self,
        inputs: StrategyInputs,
        options: StrategyOptions,
    ) -> PlanningResult:
        """Return the latest valid plan re-evaluated against current inputs."""
        return self._adapter.cached_result(inputs, options)

    def maybe_schedule(
        self,
        inputs: StrategyInputs,
        options: StrategyOptions,
        runtime_context: dict,
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
        return True

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

    async def async_shutdown(self) -> None:
        """Stop accepting results during integration unload."""
        self._closing = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

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

from .contracts import BatteryPlan, ForecastBundle, ForecastSlot, OptimizationProblem

FORECAST_TRACE_DIRECTORY = "battery_strategy_forecast_trace"
FORECAST_TRACE_SCHEMA_VERSION = 2
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
        *,
        authoritative_plan: BatteryPlan | None = None,
        optimization_problem: OptimizationProblem | None = None,
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
            coroutine = self._async_write(
                bundle, bucket_ms, authoritative_plan, optimization_problem
            )
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

    async def _async_write(
        self,
        bundle: ForecastBundle,
        bucket_ms: int,
        authoritative_plan: BatteryPlan | None = None,
        optimization_problem: OptimizationProblem | None = None,
    ) -> None:
        try:
            await self._hass.async_add_executor_job(
                self._write_if_available,
                bundle,
                bucket_ms,
                authoritative_plan,
                optimization_problem,
            )
        except Exception as err:
            self._forget(bucket_ms)
            self._warn(err)

    def _write_if_available(
        self,
        bundle: ForecastBundle,
        bucket_ms: int,
        authoritative_plan: BatteryPlan | None = None,
        optimization_problem: OptimizationProblem | None = None,
    ) -> None:
        # A writer that survived reload owns this lock until its real file I/O
        # returns. Later vintages are dropped instead of waiting in the executor.
        if not self._write_lock.acquire(blocking=False):
            return
        try:
            append_forecast_trace(
                self._root,
                bundle,
                authoritative_plan=authoritative_plan,
                optimization_problem=optimization_problem,
            )
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


def append_forecast_trace(
    root: Path,
    bundle: ForecastBundle,
    *,
    authoritative_plan: BatteryPlan | None = None,
    optimization_problem: OptimizationProblem | None = None,
) -> Path | None:
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
            return _append_forecast_trace_locked(
                root, bundle, authoritative_plan, optimization_problem
            )
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def _append_forecast_trace_locked(
    root: Path,
    bundle: ForecastBundle,
    authoritative_plan: BatteryPlan | None,
    optimization_problem: OptimizationProblem | None,
) -> Path | None:
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
        "scenarios": _serialize_scenarios(bundle),
        "optimizer_plans": {
            "authoritative": _serialize_plan(authoritative_plan),
        },
        "optimization_problem": _serialize_problem(optimization_problem),
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


def _serialize_scenarios(bundle: ForecastBundle) -> dict[str, object] | None:
    scenario_set = bundle.scenarios
    if scenario_set is None:
        return None
    return {
        "scenario_set_id": scenario_set.scenario_set_id,
        "model_version": scenario_set.model_version,
        "training_cutoff_ms": scenario_set.training_cutoff_ms,
        "paths": [
            {
                "id": scenario.scenario_id,
                "probability": scenario.probability,
                "slots": [
                    [
                        item.slot.start_ms,
                        item.load_no_ev_kwh,
                        item.pv_generation_kwh,
                        item.ev_charge_kwh,
                    ]
                    for item in scenario.slots[:FORECAST_TRACE_MAX_SLOTS]
                ],
            }
            for scenario in scenario_set.scenarios
        ],
    }


def _serialize_plan(plan: BatteryPlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    return {
        "plan_id": plan.plan_id,
        "optimizer_version": plan.optimizer_version,
        "baseline_cost_eur": plan.baseline_cost_eur,
        "optimized_cost_eur": plan.optimized_cost_eur,
        "slots": [
            [
                item.slot.start_ms,
                item.mode.value,
                item.planned_charge_kwh,
                item.planned_discharge_kwh,
                item.discharge_budget_kwh,
                item.expected_soc_start_pct,
                item.expected_soc_end_pct,
            ]
            for item in plan.slots[:FORECAST_TRACE_MAX_SLOTS]
        ],
    }


def _serialize_problem(problem: OptimizationProblem | None) -> dict[str, object] | None:
    if problem is None:
        return None
    return {
        "problem_id": problem.problem_id,
        "as_of_ms": problem.as_of_ms,
        "battery": {
            "captured_at_ms": problem.battery.captured_at_ms,
            "soc_pct": problem.battery.soc_pct,
        },
        "constraints": {
            "capacity_kwh": problem.constraints.capacity_kwh,
            "min_soc_pct": problem.constraints.min_soc_pct,
            "max_soc_pct": problem.constraints.max_soc_pct,
            "max_charge_power_w": problem.constraints.max_charge_power_w,
            "max_discharge_power_w": problem.constraints.max_discharge_power_w,
            "round_trip_efficiency": problem.constraints.round_trip_efficiency,
        },
        "commercial_policy": {
            "min_margin_ct_per_kwh": problem.policy.min_margin_ct_per_kwh,
            "terminal_value_ct_per_kwh": problem.policy.terminal_value_ct_per_kwh,
            "export_opportunity_ct_per_kwh": (
                problem.policy.export_opportunity_ct_per_kwh
            ),
            "discharge_floor_ct_per_kwh": problem.policy.discharge_floor_ct_per_kwh,
            "pv_charging_allowed": problem.policy.pv_charging_allowed,
            "grid_charging_allowed": problem.policy.grid_charging_allowed,
            "discharge_allowed": problem.policy.discharge_allowed,
            "pv_recovery_confidence": problem.policy.pv_recovery_confidence,
            "pv_recovery_reserve_kwh": problem.policy.pv_recovery_reserve_kwh,
        },
        "ev_policy": {
            "pv_to_ev_first": problem.ev_policy.pv_to_ev_first,
            "discharge_during_ev_charging": (
                problem.ev_policy.discharge_during_ev_charging
            ),
            "battery_may_feed_ev": problem.ev_policy.battery_may_feed_ev,
            "ev_active_threshold_w": problem.ev_policy.ev_active_threshold_w,
        },
        "market": [
            [
                item.slot.start_ms,
                item.import_price_ct_per_kwh,
                item.export_price_ct_per_kwh,
                item.source,
            ]
            for item in problem.market[:FORECAST_TRACE_MAX_SLOTS]
        ],
    }


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

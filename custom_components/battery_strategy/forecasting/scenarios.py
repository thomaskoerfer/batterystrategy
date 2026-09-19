"""Coherent empirical paths for scenario-based battery optimization."""

from __future__ import annotations

import datetime as dt
import statistics
from zoneinfo import ZoneInfo

from ..contracts import (
    ForecastBundle,
    ForecastScenario,
    ForecastScenarioSet,
    ForecastScenarioSlot,
    HistoricalFeatureSlot,
    QualityFlag,
)

SCENARIO_MODEL_VERSION = "empirical-weekly-path-v1"
DEFAULT_MINIMUM_PATHS = 4
DEFAULT_MAXIMUM_PATHS = 12
SLOT_H = 0.25
_INVALID_FLAGS = frozenset(
    {
        QualityFlag.MISSING_GRID,
        QualityFlag.MISSING_PV,
        QualityFlag.MISSING_BATTERY,
        QualityFlag.MISSING_EV,
        QualityFlag.RESTART_GAP,
    }
)


def build_empirical_scenarios(
    bundle: ForecastBundle,
    history: tuple[HistoricalFeatureSlot, ...],
    *,
    timezone: str,
    current_ev_charge_w: float = 0.0,
    minimum_paths: int = DEFAULT_MINIMUM_PATHS,
    maximum_paths: int = DEFAULT_MAXIMUM_PATHS,
) -> ForecastScenarioSet | None:
    """Center complete matching-week paths on current load/PV point forecasts."""
    if minimum_paths <= 0 or maximum_paths < minimum_paths:
        raise ValueError("scenario path limits are invalid")
    zone = ZoneInfo(timezone)
    eligible = tuple(item for item in history if _usable(item))
    if not eligible or not bundle.load.slots:
        return None
    by_local = {_local_key(item.slot.start_ms, zone): item for item in eligible}
    target_local = tuple(
        dt.datetime.fromtimestamp(item.slot.start_ms / 1000.0, dt.UTC).astimezone(zone)
        for item in bundle.load.slots
    )
    paths: list[tuple[int, tuple[HistoricalFeatureSlot, ...]]] = []
    for weeks_ago in range(1, 54):
        candidate = tuple(
            by_local.get(
                (
                    (local.date() - dt.timedelta(weeks=weeks_ago)).isoformat(),
                    local.hour,
                    local.minute,
                )
            )
            for local in target_local
        )
        if all(item is not None for item in candidate):
            paths.append((weeks_ago, candidate))  # type: ignore[arg-type]
        if len(paths) >= maximum_paths:
            break
    if len(paths) < minimum_paths:
        return None

    scenarios = []
    probability = 1.0 / len(paths)
    for path_index, (weeks_ago, path) in enumerate(paths):
        slots = []
        for index, historical in enumerate(path):
            load_values = tuple(item[1][index].house_load_no_ev_kwh for item in paths)
            pv_values = tuple(item[1][index].pv_generation_kwh for item in paths)
            load = max(
                0.0,
                bundle.load.slots[index].energy.p50_kwh
                + historical.house_load_no_ev_kwh
                - statistics.median(load_values),
            )
            pv = max(
                0.0,
                bundle.pv.slots[index].energy.p50_kwh
                + historical.pv_generation_kwh
                - statistics.median(pv_values),
            )
            ev = max(0.0, historical.ev_charge_kwh)
            if index == 0 and current_ev_charge_w > 0.0:
                ev = max(ev, current_ev_charge_w / 1000.0 * SLOT_H)
            slots.append(
                ForecastScenarioSlot(
                    bundle.load.slots[index].slot,
                    round(load, 6),
                    round(pv, 6),
                    round(ev, 6),
                )
            )
        scenarios.append(
            ForecastScenario(f"week-{weeks_ago}-{path_index}", probability, tuple(slots))
        )
    generated_at_ms = max(bundle.load.generated_at_ms, bundle.pv.generated_at_ms)
    cutoff_ms = max(item.slot.end_ms for item in eligible)
    return ForecastScenarioSet(
        scenario_set_id=f"{generated_at_ms}:{SCENARIO_MODEL_VERSION}",
        generated_at_ms=generated_at_ms,
        training_cutoff_ms=min(generated_at_ms, cutoff_ms),
        model_version=SCENARIO_MODEL_VERSION,
        scenarios=tuple(scenarios),
    )


def _usable(item: HistoricalFeatureSlot) -> bool:
    return item.quality.coverage >= 0.999 and not (
        frozenset(item.quality.flags) & _INVALID_FLAGS
    )


def _local_key(timestamp_ms: int, zone: ZoneInfo) -> tuple[str, int, int]:
    local = dt.datetime.fromtimestamp(timestamp_ms / 1000.0, dt.UTC).astimezone(zone)
    return local.date().isoformat(), local.hour, local.minute

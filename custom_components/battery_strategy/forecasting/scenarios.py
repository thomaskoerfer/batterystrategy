"""Coherent empirical paths for scenario-based battery optimization."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import (
    ForecastBundle,
    ForecastScenario,
    ForecastScenarioSet,
    ForecastScenarioSlot,
    HistoricalFeatureSlot,
    QualityFlag,
)
from .uncertainty import PV_SERIES, TOTAL_LOAD_SERIES, ForecastResidualCalibration

SCENARIO_MODEL_VERSION = "calibrated-weekly-copula-v1"
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


@dataclass(frozen=True, slots=True)
class ScenarioGenerationInput:
    """Captured inputs for post-publication scenario generation."""

    history: tuple[HistoricalFeatureSlot, ...]
    timezone: str
    current_ev_charge_w: float
    ev_active_threshold_w: float
    calibration: ForecastResidualCalibration
    pv_slot_cap_kwh: float

    def build(self, bundle: ForecastBundle) -> ForecastScenarioSet | None:
        """Build scenarios without reading mutable runtime state."""
        return build_empirical_scenarios(
            bundle,
            self.history,
            timezone=self.timezone,
            current_ev_charge_w=self.current_ev_charge_w,
            ev_active_threshold_w=self.ev_active_threshold_w,
            calibration=self.calibration,
            pv_slot_cap_kwh=self.pv_slot_cap_kwh,
        )


def build_empirical_scenarios(
    bundle: ForecastBundle,
    history: tuple[HistoricalFeatureSlot, ...],
    *,
    timezone: str,
    current_ev_charge_w: float = 0.0,
    ev_active_threshold_w: float = 300.0,
    calibration: ForecastResidualCalibration | None = None,
    pv_slot_cap_kwh: float | None = None,
    minimum_paths: int = DEFAULT_MINIMUM_PATHS,
    maximum_paths: int = DEFAULT_MAXIMUM_PATHS,
) -> ForecastScenarioSet | None:
    """Center complete matching-week paths on current load/PV point forecasts."""
    if minimum_paths <= 0 or maximum_paths < minimum_paths:
        raise ValueError("scenario path limits are invalid")
    zone = ZoneInfo(timezone)
    generated_at_ms = max(bundle.load.generated_at_ms, bundle.pv.generated_at_ms)
    eligible = tuple(
        item
        for item in history
        if item.slot.end_ms <= generated_at_ms and _usable(item)
    )
    if not eligible or not bundle.load.slots:
        return None
    if calibration is None:
        return None
    by_local = {_local_key(item.slot.start_ms, zone): item for item in eligible}
    target_local = tuple(
        dt.datetime.fromtimestamp(item.slot.start_ms / 1000.0, dt.UTC).astimezone(zone)
        for item in bundle.load.slots
    )
    ev_active = current_ev_charge_w >= ev_active_threshold_w
    ev_active_kwh = ev_active_threshold_w / 1000.0 * SLOT_H
    paths: list[tuple[int, tuple[HistoricalFeatureSlot, ...]]] = []
    for weeks_ago in range(1, 54):
        candidate = tuple(
            by_local.get(
                (
                    (local.date() - dt.timedelta(weeks=weeks_ago)).isoformat(),
                    local.hour,
                    local.minute,
                    local.fold,
                )
            )
            for local in target_local
        )
        candidate_active = bool(
            candidate[0] is not None and candidate[0].ev_charge_kwh >= ev_active_kwh
        )
        previous_local = target_local[0] - dt.timedelta(minutes=15)
        previous = by_local.get(
            (
                (previous_local.date() - dt.timedelta(weeks=weeks_ago)).isoformat(),
                previous_local.hour,
                previous_local.minute,
                previous_local.fold,
            )
        )
        inactive_start = bool(
            candidate_active
            and previous is not None
            and previous.ev_charge_kwh < ev_active_kwh
        )
        ev_compatible = (
            candidate_active if ev_active else not candidate_active or inactive_start
        )
        if all(item is not None for item in candidate) and ev_compatible:
            paths.append((weeks_ago, candidate))  # type: ignore[arg-type]
        if len(paths) >= maximum_paths:
            break
    if len(paths) < minimum_paths:
        return None
    scenarios = []
    probability = 1.0 / len(paths)
    for path_index, (weeks_ago, path) in enumerate(paths):
        slots = []
        current_ev_slot_kwh = max(0.0, current_ev_charge_w / 1000.0 * SLOT_H)
        historical_ev_peak = max((item.ev_charge_kwh for item in path), default=0.0)
        ev_scale = (
            current_ev_slot_kwh / historical_ev_peak
            if ev_active and historical_ev_peak > 0.0
            else 1.0
        )
        for index, historical in enumerate(path):
            load_values = tuple(item[1][index].house_load_no_ev_kwh for item in paths)
            pv_values = tuple(item[1][index].pv_generation_kwh for item in paths)
            load_point = bundle.load.slots[index].energy.p50_kwh
            pv_point = bundle.pv.slots[index].energy.p50_kwh
            local = target_local[index]
            load_residuals = calibration.residuals_for(
                TOTAL_LOAD_SERIES,
                bundle.load.model_version,
                generated_at_ms,
                bundle.load.slots[index].slot.start_ms,
                local.weekday() >= 5,
                load_point > 1e-9,
            )
            pv_residuals = calibration.residuals_for(
                PV_SERIES,
                bundle.pv.model_version,
                generated_at_ms,
                bundle.pv.slots[index].slot.start_ms,
                local.weekday() >= 5,
                pv_point > 1e-9,
            )
            if not load_residuals or not pv_residuals:
                return None
            load = load_point + _quantile(
                load_residuals,
                _rank_probability(historical.house_load_no_ev_kwh, load_values),
            )
            pv = pv_point + _quantile(
                pv_residuals,
                _rank_probability(historical.pv_generation_kwh, pv_values),
            )
            load = max(0.0, load)
            pv = max(0.0, pv)
            if pv_slot_cap_kwh is not None:
                pv = min(pv, max(0.0, pv_slot_cap_kwh))
            # Current power calibrates an active session's rate, while the
            # historical path retains start/stop and partial-slot uncertainty.
            # An inactive EV remains conditioned on inactive historical starts
            # instead of being forced inactive for the complete first slot.
            ev = max(0.0, historical.ev_charge_kwh * ev_scale)
            slots.append(
                ForecastScenarioSlot(
                    bundle.load.slots[index].slot,
                    round(load, 6),
                    round(pv, 6),
                    round(ev, 6),
                )
            )
        scenarios.append(
            ForecastScenario(
                f"week-{weeks_ago}-{path_index}", probability, tuple(slots)
            )
        )
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


def _local_key(timestamp_ms: int, zone: ZoneInfo) -> tuple[str, int, int, int]:
    local = dt.datetime.fromtimestamp(timestamp_ms / 1000.0, dt.UTC).astimezone(zone)
    return local.date().isoformat(), local.hour, local.minute, local.fold


def _rank_probability(value: float, values: tuple[float, ...]) -> float:
    lower = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    return (lower + 0.5 * max(1, equal)) / len(values)


def _quantile(values: tuple[float, ...], probability: float) -> float:
    ordered = tuple(sorted(float(item) for item in values if math.isfinite(item)))
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * max(0.0, min(1.0, probability))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

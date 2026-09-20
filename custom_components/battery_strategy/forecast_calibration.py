"""Forecast-owned maturation of issued P50 vintages into residual cohorts."""

from __future__ import annotations

import datetime as dt
import hashlib
from zoneinfo import ZoneInfo

from .contracts import ForecastDistributionBundle, HistoricalFeatureSlot, QualityFlag
from .forecasting.uncertainty import (
    PV_SERIES,
    TOTAL_LOAD_SERIES,
    ForecastResidualCalibration,
    base_model_version,
    cohort_key,
    component_series,
    lead_bucket,
)

MAX_PENDING = 1200
MAX_RESIDUALS_PER_COHORT = 96
MAX_COHORTS = 1024
MAX_COMPONENT_SERIES = 32
MISSING_ACTUAL_GRACE_MS = 6 * 60 * 60 * 1000
_LOAD_INVALID_FLAGS = frozenset(
    {
        QualityFlag.MISSING_GRID,
        QualityFlag.MISSING_PV,
        QualityFlag.MISSING_BATTERY,
        QualityFlag.MISSING_EV,
        QualityFlag.RESTART_GAP,
    }
)
_PV_INVALID_FLAGS = frozenset({QualityFlag.MISSING_PV, QualityFlag.RESTART_GAP})


def calibration_from_state(state) -> ForecastResidualCalibration:
    """Freeze persisted learned residuals for one pure forecast invocation."""
    return ForecastResidualCalibration.from_persisted(state.quantile_residuals)


def mature_predictions(
    state,
    history: tuple[HistoricalFeatureSlot, ...],
    *,
    as_of_ms: int,
    timezone: str,
) -> None:
    """Join due issued forecasts to finalized actual slots exactly once."""
    actual_by_start = {item.slot.start_ms: item for item in history}
    pending = []
    zone = ZoneInfo(timezone)
    for prediction in state.quantile_pending:
        try:
            target_ms = int(prediction.get("target_ms", -1))
            target_end_ms = int(prediction.get("target_end_ms", target_ms))
            bucket = int(prediction["lead_bucket"])
        except KeyError, TypeError, ValueError:
            continue
        if target_end_ms > as_of_ms:
            pending.append(prediction)
            continue
        actual = actual_by_start.get(target_ms)
        if actual is None:
            if target_end_ms > as_of_ms - MISSING_ACTUAL_GRACE_MS:
                pending.append(prediction)
            continue
        local = dt.datetime.fromtimestamp(target_ms / 1000.0, dt.UTC).astimezone(zone)
        is_weekend = local.weekday() >= 5
        issued_series = dict(prediction.get("series") or {})
        for series_key, issued in issued_series.items():
            if not isinstance(issued, list) or len(issued) != 2:
                continue
            actual_kwh = _actual_for_series(actual, str(series_key))
            if actual_kwh is None:
                continue
            try:
                model_version, p50_kwh = str(issued[0]), float(issued[1])
            except TypeError, ValueError:
                continue
            residual = actual_kwh - p50_kwh
            for key in (
                cohort_key(
                    str(series_key),
                    model_version,
                    bucket,
                    is_weekend,
                    p50_kwh > 1e-9,
                ),
                cohort_key(
                    str(series_key),
                    model_version,
                    bucket,
                    None,
                    p50_kwh > 1e-9,
                ),
                cohort_key(str(series_key), model_version, bucket),
            ):
                values = state.quantile_residuals.pop(key, [])
                if not isinstance(values, list):
                    values = []
                values.append(round(residual, 6))
                del values[:-MAX_RESIDUALS_PER_COHORT]
                state.quantile_residuals[key] = values
                while len(state.quantile_residuals) > MAX_COHORTS:
                    state.quantile_residuals.pop(next(iter(state.quantile_residuals)))
    state.quantile_pending = pending[-MAX_PENDING:]


def queue_predictions(state, bundle: ForecastDistributionBundle) -> None:
    """Freeze one production P50 per target and lead class without duplication."""
    existing = {
        (
            int(item.get("target_ms", -1)),
            int(item.get("lead_bucket", -1)),
            str(item.get("model_signature", "")),
        )
        for item in state.quantile_pending
    }
    components = {
        item.component_key: item
        for item in sorted(
            (
                value
                for value in bundle.load.components
                if value.component_key != "general_house_load"
            ),
            key=lambda value: value.component_key,
        )[:MAX_COMPONENT_SERIES]
    }
    model_signature = _model_signature(bundle, components)
    generated_at_ms = max(bundle.load.generated_at_ms, bundle.pv.generated_at_ms)
    for index, load_slot in enumerate(bundle.load.slots):
        bucket = lead_bucket(generated_at_ms, load_slot.slot.start_ms)
        identity = (load_slot.slot.start_ms, bucket, model_signature)
        if identity in existing:
            continue
        pv_slot = bundle.pv.slots[index]
        series = {
            TOTAL_LOAD_SERIES: [
                base_model_version(bundle.load.model_version),
                load_slot.energy.p50_kwh,
            ],
            PV_SERIES: [
                base_model_version(bundle.pv.model_version),
                pv_slot.energy.p50_kwh,
            ],
        }
        for key, component in components.items():
            series[component_series(key)] = [
                base_model_version(component.model_version),
                component.slots[index].energy.p50_kwh,
            ]
        state.quantile_pending.append(
            {
                "target_ms": load_slot.slot.start_ms,
                "target_end_ms": load_slot.slot.end_ms,
                "generated_at_ms": generated_at_ms,
                "lead_bucket": bucket,
                "model_signature": model_signature,
                "series": series,
            }
        )
        existing.add(identity)
    state.quantile_pending = state.quantile_pending[-MAX_PENDING:]


def _model_signature(bundle: ForecastDistributionBundle, components: dict) -> str:
    versions = [
        f"{TOTAL_LOAD_SERIES}:{base_model_version(bundle.load.model_version)}",
        f"{PV_SERIES}:{base_model_version(bundle.pv.model_version)}",
        *(
            f"{component_series(key)}:{base_model_version(component.model_version)}"
            for key, component in components.items()
        ),
    ]
    return hashlib.sha256("|".join(versions).encode()).hexdigest()[:12]


def _actual_for_series(
    actual: HistoricalFeatureSlot,
    series_key: str,
) -> float | None:
    if series_key == TOTAL_LOAD_SERIES:
        invalid = frozenset(actual.quality.flags) & _LOAD_INVALID_FLAGS
        return (
            max(0.0, actual.house_load_no_ev_kwh)
            if actual.quality.coverage >= 0.999 and not invalid
            else None
        )
    if series_key == PV_SERIES:
        invalid = frozenset(actual.quality.flags) & _PV_INVALID_FLAGS
        return (
            max(0.0, actual.pv_generation_kwh)
            if actual.quality.coverage >= 0.999 and not invalid
            else None
        )
    component_key = series_key.removeprefix("component:")
    component = next(
        (
            item
            for item in actual.load_components
            if item.component_key == component_key
        ),
        None,
    )
    if (
        component is None
        or component.quality.coverage < 0.999
        or component.quality.flags
    ):
        return None
    return max(0.0, component.energy_kwh)

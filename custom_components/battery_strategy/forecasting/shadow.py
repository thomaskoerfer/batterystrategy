"""Recorder-independent shadow evaluation using finalized feature slots."""

from __future__ import annotations

from ..contracts import (
    ForecastBundle,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadForecastContext,
    QualityFlag,
)
from .legacy import (
    LegacyForecastConfig,
    LegacyForecastSample,
    LegacyForecastTarget,
)
from .load import build_legacy_load_forecast
from .pv import build_legacy_pv_forecast

SLOT_H = 0.25

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
_EVALUATION_HISTORY_SLOTS = 7 * 96


def evaluate_feature_store_shadow(
    *,
    production: ForecastBundle,
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    targets: tuple[LegacyForecastTarget, ...],
    context: LoadForecastContext,
    config: LegacyForecastConfig,
) -> dict[str, object]:
    """Compare feature-store and production forecasts without affecting either."""
    eligible = tuple(slot for slot in history if slot.slot.end_ms <= request.as_of_ms)
    samples = _feature_samples(eligible)
    load_usable = sum(sample.load_valid for sample in samples)
    pv_usable = sum(sample.pv_valid for sample in samples)
    history_start_ms = eligible[0].slot.start_ms if eligible else None
    history_end_ms = eligible[-1].slot.end_ms if eligible else None
    history_span_days = (
        round((history_end_ms - history_start_ms) / 86_400_000.0, 3)
        if history_start_ms is not None and history_end_ms is not None
        else 0.0
    )
    ready = (
        load_usable >= _EVALUATION_HISTORY_SLOTS
        and pv_usable >= _EVALUATION_HISTORY_SLOTS
        and history_span_days >= 7.0
    )
    if not samples:
        return {
            "generated_at_ms": request.as_of_ms,
            "status": "warming_up",
            "reason": "no_eligible_feature_slots",
            "authoritative": False,
            "history_slot_count": len(eligible),
            "load_usable_slots": load_usable,
            "pv_usable_slots": pv_usable,
            "history_span_days": history_span_days,
        }

    load_shadow = None
    pv_shadow = None
    component_errors = {}
    try:
        load_shadow = build_legacy_load_forecast(
            request, samples, targets, context, config.load_config()
        )
    except Exception as err:  # noqa: BLE001 - component diagnostics stay isolated.
        component_errors["load"] = f"{type(err).__name__}: {err}"
    try:
        pv_shadow = build_legacy_pv_forecast(
            request, samples, targets, config.pv_config()
        )
    except Exception as err:  # noqa: BLE001 - component diagnostics stay isolated.
        component_errors["pv"] = f"{type(err).__name__}: {err}"

    evaluation_index = next(
        (
            index
            for index, item in enumerate(request.slots)
            if item.start_ms >= _next_slot_start(request.as_of_ms)
        ),
        None,
    )
    comparison: dict[str, object] = {
        "generated_at_ms": request.as_of_ms,
        "status": (
            "component_error"
            if component_errors
            else "ready"
            if ready
            else "warming_up"
        ),
        "reason": (
            component_errors
            if component_errors
            else None
            if ready
            else "insufficient_finalized_history"
        ),
        "authoritative": False,
        "history_slot_count": len(eligible),
        "load_usable_slots": load_usable,
        "pv_usable_slots": pv_usable,
        "history_span_days": history_span_days,
        "slot_count": len(request.slots),
    }
    if load_shadow is not None:
        _require_same_grid(production.load.slots, load_shadow.slots, "load")
        load = _series_comparison(production.load.slots, load_shadow.slots)
        comparison.update({f"load_{key}": value for key, value in load.items()})
    if pv_shadow is not None:
        _require_same_grid(production.pv.slots, pv_shadow.slots, "PV")
        pv = _series_comparison(production.pv.slots, pv_shadow.slots)
        comparison.update({f"pv_{key}": value for key, value in pv.items()})
    if (
        evaluation_index is not None
        and load_shadow is not None
        and pv_shadow is not None
    ):
        production_load = production.load.slots[evaluation_index]
        shadow_load = load_shadow.slots[evaluation_index]
        production_pv = production.pv.slots[evaluation_index]
        shadow_pv = pv_shadow.slots[evaluation_index]
        comparison.update(
            {
                "evaluation_slot_start_ms": production_load.slot.start_ms,
                "production_load_kwh": production_load.energy.p50_kwh,
                "shadow_load_kwh": shadow_load.energy.p50_kwh,
                "production_pv_kwh": production_pv.energy.p50_kwh,
                "shadow_pv_kwh": shadow_pv.energy.p50_kwh,
            }
        )
    return comparison


def _require_same_grid(production_slots, shadow_slots, component: str) -> None:
    if tuple(item.slot for item in production_slots) != tuple(
        item.slot for item in shadow_slots
    ):
        raise ValueError(f"shadow {component} grid differs from production")


def _feature_samples(
    history: tuple[HistoricalFeatureSlot, ...],
) -> tuple[LegacyForecastSample, ...]:
    samples = []
    for item in history[-6000:]:
        flags = frozenset(item.quality.flags)
        load_valid = item.quality.coverage >= 0.999 and not flags & _LOAD_INVALID_FLAGS
        pv_valid = item.quality.coverage >= 0.999 and not flags & _PV_INVALID_FLAGS
        samples.append(
            LegacyForecastSample(
                ts_s=item.slot.start_ms / 1000.0,
                load_w=item.house_load_no_ev_kwh / SLOT_H * 1000.0,
                pv_w=item.pv_generation_kwh / SLOT_H * 1000.0,
                grid_import_w=item.grid_import_kwh / SLOT_H * 1000.0,
                grid_export_w=item.grid_export_kwh / SLOT_H * 1000.0,
                load_valid=load_valid,
                pv_valid=pv_valid,
            )
        )
    return tuple(samples)


def _series_comparison(production_slots, shadow_slots) -> dict[str, float]:
    deltas_w = [
        (shadow.energy.p50_kwh - production.energy.p50_kwh) / SLOT_H * 1000.0
        for production, shadow in zip(production_slots, shadow_slots, strict=True)
    ]
    return {
        "mae_delta_w": round(sum(abs(value) for value in deltas_w) / len(deltas_w), 3),
        "bias_delta_w": round(sum(deltas_w) / len(deltas_w), 3),
        "max_delta_w": round(max(abs(value) for value in deltas_w), 3),
        "horizon_delta_kwh": round(sum(deltas_w) * SLOT_H / 1000.0, 4),
    }


def _next_slot_start(as_of_ms: int) -> int:
    slot_ms = int(SLOT_H * 3_600_000)
    return (int(as_of_ms) // slot_ms + 1) * slot_ms

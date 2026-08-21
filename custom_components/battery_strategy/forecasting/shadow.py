"""Recorder-independent shadow evaluation using finalized feature slots."""

from __future__ import annotations

import datetime as dt

from ..contracts import (
    ForecastBundle,
    ForecastEvaluationPoint,
    ForecastEvaluationRun,
    ForecastRequest,
    ForecastSlot,
    HistoricalFeatureSlot,
    LoadDriverSnapshot,
    LoadForecast,
    LoadForecastContext,
    PvForecast,
    QualityFlag,
    QuantileEnergy,
    SlotKey,
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
_EVALUATION_LEAD_MINUTES = (15, 60, 360, 720, 1440)


def evaluate_feature_store_shadow_snapshot(
    snapshot: dict[str, object], history: tuple[HistoricalFeatureSlot, ...]
) -> ForecastEvaluationRun:
    """Evaluate an optimizer-produced forecast snapshot against feature history."""
    request_data = snapshot["request"]
    production_data = snapshot["production"]
    context_data = snapshot["context"]
    config_data = snapshot["config"]
    request = ForecastRequest(
        int(request_data["as_of_ms"]),
        str(request_data["timezone"]),
        tuple(SlotKey(int(item[0]), int(item[1])) for item in request_data["slots"]),
    )
    load_slots = tuple(
        ForecastSlot(slot, QuantileEnergy(float(value)))
        for slot, value in zip(
            request.slots, production_data["load"], strict=True
        )
    )
    pv_slots = tuple(
        ForecastSlot(slot, QuantileEnergy(float(value)))
        for slot, value in zip(request.slots, production_data["pv"], strict=True)
    )
    production = ForecastBundle(
        LoadForecast(
            f"production-{request.as_of_ms}-load",
            request.as_of_ms,
            int(production_data["load_training_cutoff_ms"]),
            "production-load-v1",
            load_slots,
        ),
        PvForecast(
            f"production-{request.as_of_ms}-pv",
            request.as_of_ms,
            int(production_data["pv_training_cutoff_ms"]),
            "production-pv-v1",
            pv_slots,
        ),
    )
    context = LoadForecastContext(
        float(context_data["house_load_no_ev_w"]),
        tuple(
            LoadDriverSnapshot(str(item[0]), float(item[1]))
            for item in context_data.get("drivers", [])
        ),
    )
    config = LegacyForecastConfig(
        timezone=str(config_data["timezone"]),
        load_bias=float(config_data["load_bias"]),
        load_slot_biases=tuple(float(value) for value in config_data["load_slot_biases"]),
        pv_global_bias=float(config_data["pv_global_bias"]),
        pv_slot_biases=tuple(float(value) for value in config_data["pv_slot_biases"]),
        current_weather_factor=float(config_data["current_weather_factor"]),
        current_pv_w=(
            None
            if config_data.get("current_pv_w") is None
            else float(config_data["current_pv_w"])
        ),
        tomorrow_date=str(config_data["tomorrow_date"]),
        tomorrow_energy_kwh=(
            None
            if config_data.get("tomorrow_energy_kwh") is None
            else float(config_data["tomorrow_energy_kwh"])
        ),
        pv_capacity_kwp=float(config_data["pv_capacity_kwp"]),
        pv_inverter_kw=float(config_data["pv_inverter_kw"]),
    )
    targets = tuple(
        LegacyForecastTarget(dt.datetime.fromisoformat(str(item[0])), float(item[1]))
        for item in snapshot["targets"]
    )
    return evaluate_feature_store_shadow(
        production=production,
        request=request,
        history=history,
        targets=targets,
        context=context,
        config=config,
    )


def evaluate_feature_store_shadow(
    *,
    production: ForecastBundle,
    request: ForecastRequest,
    history: tuple[HistoricalFeatureSlot, ...],
    targets: tuple[LegacyForecastTarget, ...],
    context: LoadForecastContext,
    config: LegacyForecastConfig,
) -> ForecastEvaluationRun:
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
        return ForecastEvaluationRun(
            request.as_of_ms,
            "warming_up",
            "no_eligible_feature_slots",
            len(eligible),
            load_usable,
            pv_usable,
            history_span_days,
            None,
            None,
            None,
            None,
        )

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

    load_metrics = None
    pv_metrics = None
    if load_shadow is not None:
        _require_same_grid(production.load.slots, load_shadow.slots, "load")
        load_metrics = _series_comparison(production.load.slots, load_shadow.slots)
    if pv_shadow is not None:
        _require_same_grid(production.pv.slots, pv_shadow.slots, "PV")
        pv_metrics = _series_comparison(production.pv.slots, pv_shadow.slots)
    points = ()
    if load_shadow is not None and pv_shadow is not None:
        points = _evaluation_points(
            request.as_of_ms, production, load_shadow, pv_shadow
        )
    return ForecastEvaluationRun(
        generated_at_ms=request.as_of_ms,
        status=(
            "component_error"
            if component_errors
            else "ready"
            if ready
            else "warming_up"
        ),
        reason=(
            "; ".join(f"{key}: {value}" for key, value in component_errors.items())
            if component_errors
            else None
            if ready
            else "insufficient_finalized_history"
        ),
        history_slot_count=len(eligible),
        load_usable_slots=load_usable,
        pv_usable_slots=pv_usable,
        history_span_days=history_span_days,
        load_parity_mae_w=(load_metrics or {}).get("mae_delta_w"),
        load_parity_bias_w=(load_metrics or {}).get("bias_delta_w"),
        pv_parity_mae_w=(pv_metrics or {}).get("mae_delta_w"),
        pv_parity_bias_w=(pv_metrics or {}).get("bias_delta_w"),
        points=points,
    )


def _evaluation_points(as_of_ms, production, load_shadow, pv_shadow):
    points = []
    used_indexes = set()
    for lead_minutes in _EVALUATION_LEAD_MINUTES:
        target_ms = as_of_ms + lead_minutes * 60_000
        candidates = [
            (abs(item.slot.start_ms - target_ms), index)
            for index, item in enumerate(production.load.slots)
            if item.slot.start_ms > as_of_ms
        ]
        if not candidates:
            continue
        distance, index = min(candidates)
        if distance > 15 * 60_000 or index in used_indexes:
            continue
        used_indexes.add(index)
        points.append(
            ForecastEvaluationPoint(
                generated_at_ms=as_of_ms,
                target=production.load.slots[index].slot,
                lead_minutes=lead_minutes,
                production_load_kwh=production.load.slots[index].energy.p50_kwh,
                shadow_load_kwh=load_shadow.slots[index].energy.p50_kwh,
                production_pv_kwh=production.pv.slots[index].energy.p50_kwh,
                shadow_pv_kwh=pv_shadow.slots[index].energy.p50_kwh,
            )
        )
    return tuple(points)


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

"""Online forecast calibration and bounded evaluation metrics."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .planning_state import ForecastLearningState

BIAS_ALPHA = 0.12
SLOT_BIAS_ALPHA = 0.08


@dataclass(frozen=True, slots=True)
class ForecastEvaluationSummary:
    """Recent evaluation windows after matured predictions are processed."""

    last_24h: tuple[dict[str, Any], ...]
    last_7d: tuple[dict[str, Any], ...]
    hit_rate_24h_pct: float | None

    def mean_24h(self, key: str) -> float | None:
        return _mean(self.last_24h, key)

    def mean_7d(self, key: str) -> float | None:
        return _mean(self.last_7d, key)


def _clamp(value, lower, upper):
    return max(lower, min(upper, value))


def _slot_index(local_dt: dt.datetime) -> int:
    return local_dt.hour * 4 + local_dt.minute // 15


def _average(samples, start_ts, end_ts, key):
    values = [
        sample.get(key, 0.0)
        for sample in samples
        if start_ts <= sample.get("ts", 0) <= end_ts
    ]
    return sum(values) / len(values) if values else None


def _mean(items, key):
    return sum(item.get(key, 0.0) for item in items) / len(items) if items else None


def update_forecast_evaluation(
    state: ForecastLearningState,
    *,
    now_ts,
    local_timezone,
    round_trip_efficiency,
    retention_days,
) -> ForecastEvaluationSummary:
    """Mature predictions and update only forecast-owned learning state."""
    due = [item for item in state.predictions if item.get("target_ts", 0) <= now_ts]
    state.predictions = [
        item for item in state.predictions if item.get("target_ts", 0) > now_ts
    ][-1200:]

    for prediction in due:
        end_ts = prediction["target_ts"]
        start_ts = end_ts - 3600
        slot = _slot_index(dt.datetime.fromtimestamp(end_ts, tz=local_timezone))
        pv_avg = _average(state.samples, start_ts, end_ts, "pv_w")
        load_avg = _average(state.samples, start_ts, end_ts, "load_w")
        price_target = _average(state.samples, end_ts - 900, end_ts + 900, "price_ct")
        if pv_avg is None or load_avg is None or price_target is None:
            continue

        pv_actual = max(0.0, pv_avg) / 1000.0
        load_actual = max(0.0, load_avg) / 1000.0
        pv_error = abs(prediction.get("pv_pred_kwh", 0.0) - pv_actual)
        load_error = abs(prediction.get("load_pred_kwh", 0.0) - load_actual)

        pv_prediction = max(0.05, float(prediction.get("pv_pred_kwh", 0.0)))
        if pv_actual > 0.02:
            pv_ratio = _clamp(pv_actual / pv_prediction, 0.7, 1.3)
            state.pv_bias = _clamp(
                (1.0 - BIAS_ALPHA) * float(state.pv_bias) + BIAS_ALPHA * pv_ratio,
                0.5,
                1.6,
            )
            previous = state.pv_bias_slots[slot]
            state.pv_bias_slots[slot] = _clamp(
                (1.0 - SLOT_BIAS_ALPHA) * previous + SLOT_BIAS_ALPHA * pv_ratio,
                0.5,
                1.6,
            )

        load_prediction = max(0.2, float(prediction.get("load_pred_kwh", 0.0)))
        load_ratio = _clamp(load_actual / load_prediction, 0.75, 1.25)
        state.load_bias = _clamp(
            (1.0 - BIAS_ALPHA) * float(state.load_bias) + BIAS_ALPHA * load_ratio,
            0.6,
            1.6,
        )
        previous = state.load_bias_slots[slot]
        state.load_bias_slots[slot] = _clamp(
            (1.0 - SLOT_BIAS_ALPHA) * previous + SLOT_BIAS_ALPHA * load_ratio,
            0.6,
            1.6,
        )

        success = True
        if prediction.get("mode") == "charge_grid":
            success = price_target * round_trip_efficiency > prediction.get(
                "price_ct", 0.0
            )
        elif str(prediction.get("mode", "")).startswith("discharge_"):
            success = (
                prediction.get("price_ct", 0.0) * round_trip_efficiency > price_target
            )

        state.backtests.append(
            {
                "ts": end_ts,
                "pv_mae": pv_error,
                "load_mae": load_error,
                "success": bool(success),
                "pv_bias_after": round(float(state.pv_bias), 4),
                "load_bias_after": round(float(state.load_bias), 4),
            }
        )

    cutoff = now_ts - retention_days * 86400
    state.backtests = [item for item in state.backtests if item.get("ts", 0) >= cutoff][
        -8000:
    ]
    last_24h = tuple(
        item for item in state.backtests if item.get("ts", 0) >= now_ts - 86400
    )
    last_7d = tuple(
        item for item in state.backtests if item.get("ts", 0) >= now_ts - 7 * 86400
    )
    hit_rate = (
        100.0 * sum(1 for item in last_24h if item.get("success")) / len(last_24h)
        if last_24h
        else None
    )
    return ForecastEvaluationSummary(last_24h, last_7d, hit_rate)

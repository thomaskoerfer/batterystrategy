"""Independent EV-free house-load forecasting."""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import (
    ForecastRequest,
    ForecastSlot,
    LoadForecast,
    LoadForecastComponent,
    LoadForecastContext,
    QuantileEnergy,
)
from .history import LegacyForecastSample, LegacyForecastTarget

SLOT_H = 0.25


@dataclass(frozen=True, slots=True)
class LegacyLoadForecastConfig:
    timezone: str
    load_bias: float
    load_slot_biases: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.load_slot_biases) != 96:
            raise ValueError("load slot bias array must contain 96 values")


def build_legacy_load_forecast(
    request: ForecastRequest,
    samples: tuple[LegacyForecastSample, ...],
    targets: tuple[LegacyForecastTarget, ...],
    context: LoadForecastContext,
    config: LegacyLoadForecastConfig,
) -> LoadForecast:
    """Forecast EV-free load without importing PV configuration or logic."""
    if len(request.slots) != len(targets):
        raise ValueError("load targets must match requested grid")
    timezone = ZoneInfo(config.timezone)
    heat_pump_w = next(
        (
            driver.power_w
            for driver in context.drivers
            if driver.driver_key == "heat_pump"
        ),
        0.0,
    )
    now_local = dt.datetime.fromtimestamp(
        request.as_of_ms / 1000.0, tz=dt.timezone.utc
    ).astimezone(timezone)
    slots = tuple(
        ForecastSlot(
            slot_key,
            QuantileEnergy(
                _forecast_load_w(
                    samples,
                    target.local_start,
                    timezone,
                    heat_pump_w,
                    now_local,
                    config.load_bias,
                    config.load_slot_biases[_slot_index(target.local_start)],
                )
                * SLOT_H
                / 1000.0
            ),
        )
        for slot_key, target in zip(request.slots, targets, strict=True)
    )
    cutoff = min(
        request.as_of_ms,
        int(max((sample.ts_s for sample in samples), default=0.0) * 1000),
    )
    return LoadForecast(
        forecast_id=f"legacy-{request.as_of_ms}-load",
        generated_at_ms=request.as_of_ms,
        training_cutoff_ms=cutoff,
        model_version="legacy-load-v1",
        slots=slots,
        components=(
            LoadForecastComponent("general_house_load", "legacy-load-v1", slots),
        ),
    )


def _forecast_load_w(
    samples, target, timezone, heat_pump_w, now_local, load_bias, slot_bias
) -> float:
    target_slot = _slot_index(target)
    target_weekday = target.weekday()
    target_is_weekend = target_weekday >= 5
    same_slot, same_weekday, same_weektype, recent = [], [], [], []
    recent_cutoff = samples[-1].ts_s - 7200 if samples else 0.0
    for sample in samples[-6000:]:
        sample_local = dt.datetime.fromtimestamp(
            sample.ts_s, tz=dt.timezone.utc
        ).astimezone(timezone)
        if not sample.load_valid or not sample.has_valid_live_power:
            continue
        if _slot_index(sample_local) == target_slot:
            same_slot.append(sample.load_w)
            if sample_local.weekday() == target_weekday:
                same_weekday.append(sample.load_w)
            if (sample_local.weekday() >= 5) == target_is_weekend:
                same_weektype.append(sample.load_w)
        if sample.ts_s >= recent_cutoff:
            recent.append(sample.load_w)
    base_all = _median(same_slot[-60:], 450.0)
    base_weekday = _median(same_weekday[-20:], base_all)
    base_weektype = _median(same_weektype[-30:], base_all)
    trend = sum(recent) / len(recent) if recent else base_all
    load_w = 0.45 * base_weekday + 0.25 * base_weektype + 0.15 * base_all + 0.15 * trend
    horizon_h = max(0.0, (target - now_local).total_seconds() / 3600.0)
    if horizon_h <= 6.0:
        load_w += 0.22 * max(0.0, heat_pump_w - 500.0) * math.exp(-horizon_h / 1.5)
    return max(0.0, load_w * _clamp(load_bias, 0.6, 1.6) * _clamp(slot_bias, 0.7, 1.4))


def _slot_index(value: dt.datetime) -> int:
    return value.hour * 4 + value.minute // 15


def _median(values, fallback) -> float:
    return float(statistics.median(values)) if values else fallback


def _clamp(value, low, high) -> float:
    return max(low, min(high, float(value)))

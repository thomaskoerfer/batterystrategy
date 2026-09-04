"""Independent PV-generation forecasting."""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import ForecastRequest, ForecastSlot, PvForecast, QuantileEnergy
from .history import ForecastHistorySample, ForecastTargetInput

SLOT_H = 0.25
PV_NOWCAST_BLEND_HOURS = 2.5


@dataclass(frozen=True, slots=True)
class PvForecastModelConfig:
    timezone: str
    pv_global_bias: float
    pv_slot_biases: tuple[float, ...]
    current_weather_factor: float
    current_pv_w: float | None
    tomorrow_date: str
    tomorrow_energy_kwh: float | None
    pv_capacity_kwp: float
    pv_inverter_kw: float

    def __post_init__(self) -> None:
        if len(self.pv_slot_biases) != 96:
            raise ValueError("PV slot bias array must contain 96 values")
        if self.pv_capacity_kwp <= 0.0 or self.pv_inverter_kw <= 0.0:
            raise ValueError("current PV and inverter capacity are required")


def build_pv_forecast(
    request: ForecastRequest,
    samples: tuple[ForecastHistorySample, ...],
    targets: tuple[ForecastTargetInput, ...],
    config: PvForecastModelConfig,
) -> PvForecast:
    """Forecast PV without importing load context or load-model logic."""
    if len(request.slots) != len(targets):
        raise ValueError("PV targets must match requested grid")
    timezone = ZoneInfo(config.timezone)
    now_local = dt.datetime.fromtimestamp(
        request.as_of_ms / 1000.0, tz=dt.UTC
    ).astimezone(timezone)
    preliminary = [
        (
            _forecast_pv_w(
                samples,
                target.local_start,
                timezone,
                target.weather_factor * config.pv_global_bias,
                config.pv_slot_biases[_slot_index(target.local_start)],
                config.pv_inverter_kw,
            ),
            target,
        )
        for target in targets
    ]
    if config.current_pv_w is not None and config.current_pv_w > 50.0:
        current_weather_ref = max(0.2, config.current_weather_factor)
        anchored = []
        for pv_w, target in preliminary:
            horizon_h = max(
                0.0, (target.local_start - now_local).total_seconds() / 3600.0
            )
            if horizon_h <= PV_NOWCAST_BLEND_HOURS and pv_w > 1.0:
                blend = 1.0 - horizon_h / PV_NOWCAST_BLEND_HOURS
                persistence = max(
                    0.0,
                    config.current_pv_w
                    * (max(0.2, target.weather_factor) / current_weather_ref),
                )
                pv_w = max(pv_w, pv_w * (1.0 - blend) + persistence * blend)
            anchored.append((pv_w, target))
        preliminary = anchored
    scale = _tomorrow_scale(preliminary, config)
    slots = tuple(
        ForecastSlot(
            slot_key,
            QuantileEnergy(
                max(
                    0.0,
                    pv_w
                    * (
                        scale
                        if target.local_start.date().isoformat() == config.tomorrow_date
                        else 1.0
                    ),
                )
                * SLOT_H
                / 1000.0
            ),
        )
        for slot_key, (pv_w, target) in zip(request.slots, preliminary, strict=True)
    )
    cutoff = min(
        request.as_of_ms,
        int(max((sample.ts_s for sample in samples), default=0.0) * 1000),
    )
    return PvForecast(
        f"slot-profile-{request.as_of_ms}-pv",
        request.as_of_ms,
        cutoff,
        "slot-profile-pv-v1",
        slots,
    )


def _forecast_pv_w(
    samples, target, timezone, weather_factor, slot_bias, inverter_kw
) -> float:
    target_slot = _slot_index(target)
    target_is_weekend = target.weekday() >= 5
    same_slot, same_weektype = [], []
    for sample in samples[-6000:]:
        sample_local = dt.datetime.fromtimestamp(sample.ts_s, tz=dt.UTC).astimezone(
            timezone
        )
        if (
            not sample.pv_valid
            or (not sample.has_valid_live_power and sample.pv_w <= 1.0)
            or _slot_index(sample_local) != target_slot
        ):
            continue
        same_slot.append(sample.pv_w)
        if (sample_local.weekday() >= 5) == target_is_weekend:
            same_weektype.append(sample.pv_w)
    pv_w = 0.65 * _median(
        same_weektype[-30:], _median(same_slot[-60:], 0.0)
    ) + 0.35 * _median(same_slot[-60:], 0.0)
    if target.hour < 6 or target.hour > 20:
        pv_w = 0.0
    return min(
        max(0.0, pv_w * weather_factor * _clamp(slot_bias, 0.6, 1.5)),
        max(0.0, inverter_kw * 1000.0),
    )


def _tomorrow_scale(preliminary, config) -> float:
    if config.tomorrow_energy_kwh is None or config.tomorrow_energy_kwh <= 0.0:
        return 1.0
    forecast_kwh = sum(
        max(0.0, pv_w) * SLOT_H / 1000.0
        for pv_w, target in preliminary
        if target.local_start.date().isoformat() == config.tomorrow_date
    )
    return (
        1.0
        if forecast_kwh <= 0.05
        else _clamp(config.tomorrow_energy_kwh / forecast_kwh, 0.25, 4.0)
    )


def _slot_index(value) -> int:
    return value.hour * 4 + value.minute // 15


def _median(values, fallback) -> float:
    return float(statistics.median(values)) if values else fallback


def _clamp(value, low, high) -> float:
    return max(low, min(high, float(value)))

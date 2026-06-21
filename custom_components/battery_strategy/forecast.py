"""Forecast helpers for Battery Strategy planning."""

from __future__ import annotations

import datetime as dt
import math

from .models import StrategyInputs, StrategyOptions
from .plan_models import ForecastPoint, PricePoint
from .pricing import price_at
from .strategy import current_house_loads_w

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 4


def build_forecast_points(
    inputs: StrategyInputs,
    options: StrategyOptions,
    now: dt.datetime,
    prices: list[PricePoint],
) -> list[ForecastPoint]:
    """Build a conservative 48h forecast from live inputs and available prices."""
    horizon_h = max(1, int(options.planning_horizon_h))
    slots = horizon_h * SLOTS_PER_HOUR
    house_total, house_no_ev = current_house_loads_w(inputs, options)
    base_load = max(150.0, house_no_ev)
    live_pv = max(0.0, float(inputs.pv_w))
    points: list[ForecastPoint] = []
    for idx in range(slots):
        slot_dt = _floor_quarter(now) + dt.timedelta(minutes=SLOT_MINUTES * idx)
        ts_ms = int(slot_dt.timestamp() * 1000)
        pv = _pv_shape(slot_dt, live_pv)
        load = _load_shape(slot_dt, base_load)
        points.append(ForecastPoint(ts_ms, load, pv, price_at(prices, ts_ms)))
    return points


def forecast_energy_kwh(points: list[ForecastPoint], key: str, hours: int = 1) -> float:
    """Return forecast energy for the next N hours."""
    selected = points[: max(0, hours * SLOTS_PER_HOUR)]
    return round(sum(max(0.0, float(getattr(p, key))) for p in selected) / 1000.0 / SLOTS_PER_HOUR, 3)


def clamp_bias(value: float, low: float, high: float) -> float:
    """Clamp forecast calibration bias."""
    return max(low, min(high, float(value)))


def fallback_weather_factor(cloud_cover: float | None = None, shortwave_radiation: float | None = None) -> float:
    """Return a stable weather factor when no provider data is available."""
    if shortwave_radiation is not None and shortwave_radiation > 0:
        return clamp_bias(float(shortwave_radiation) / 650.0, 0.15, 1.25)
    if cloud_cover is None:
        return 0.75
    return clamp_bias(1.0 - (float(cloud_cover) / 100.0) * 0.75, 0.15, 1.0)


def _floor_quarter(now: dt.datetime) -> dt.datetime:
    minute = (now.minute // SLOT_MINUTES) * SLOT_MINUTES
    return now.replace(minute=minute, second=0, microsecond=0)


def _load_shape(slot_dt: dt.datetime, base_load_w: float) -> float:
    hour = slot_dt.hour + slot_dt.minute / 60.0
    morning = 1.0 + 0.18 * math.exp(-((hour - 7.5) / 2.0) ** 2)
    evening = 1.0 + 0.25 * math.exp(-((hour - 19.0) / 3.0) ** 2)
    night = 0.88 if hour < 5.0 else 1.0
    return max(120.0, base_load_w * max(morning, evening) * night)


def _pv_shape(slot_dt: dt.datetime, live_pv_w: float) -> float:
    hour = slot_dt.hour + slot_dt.minute / 60.0
    if hour < 5.5 or hour > 21.5:
        return 0.0
    daylight = math.sin(math.pi * (hour - 5.5) / 16.0)
    seasonal_peak = max(live_pv_w, 1800.0)
    return max(0.0, seasonal_peak * max(0.0, daylight) ** 1.7)

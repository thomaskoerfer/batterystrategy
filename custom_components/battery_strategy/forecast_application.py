"""Forecast application boundary for a planning refresh."""

from __future__ import annotations

import datetime as dt
import time

from .contracts import ForecastRequest, LoadForecastContext, SlotKey
from .forecasting import (
    FeatureStoreForecastNotReady,
    ForecastModelConfig,
    ForecastTargetInput,
    build_feature_store_forecast,
    feature_store_forecast_readiness,
)

SLOT_H = 0.25


def _clamp(value, lower, upper):
    return max(lower, min(upper, value))


def bootstrap_samples_from_features(runtime, now_ts, days=21):
    """Build calibration samples from canonical finalized features."""
    cutoff_ms = int((float(now_ts) - days * 86400) * 1000)
    samples = []
    for feature in runtime.forecast_history:
        if feature.slot.start_ms < cutoff_ms:
            continue
        factor_w = 1000.0 / SLOT_H
        battery_power_w = (
            feature.battery_discharge_kwh - feature.battery_charge_kwh
        ) * factor_w
        ev_w = feature.ev_charge_kwh * factor_w
        house_w = feature.house_load_no_ev_kwh * factor_w
        samples.append(
            {
                "ts": feature.slot.start_ms / 1000.0,
                "load_w": house_w,
                "house_w": house_w,
                "house_total_w": house_w + ev_w,
                "wallbox_w": ev_w,
                "grid_import_w": feature.grid_import_kwh * factor_w,
                "grid_export_w": feature.grid_export_kwh * factor_w,
                "pv_w": feature.pv_generation_kwh * factor_w,
                "bat_in_out_w": battery_power_w,
                "hp_w": 0.0,
                "price_ct": float(feature.price_ct_per_kwh or 0.0),
                "soc": -1,
            }
        )
    return samples[-12000:]


def weather_factor_from_cloud_rad(cloud_cover, shortwave_radiation):
    cloud_factor = _clamp(1.0 - float(cloud_cover or 0.0) / 130.0, 0.35, 1.05)
    rad_factor = _clamp(float(shortwave_radiation or 0.0) / 650.0, 0.2, 1.1)
    return 0.6 * cloud_factor + 0.4 * rad_factor


def weather_snapshot(runtime, now_ts_ms):
    """Project the canonical slot weather into the published runtime shape."""
    slots = runtime.forecast_weather
    if not slots:
        return None
    current = next(
        (item for item in slots if item.slot.start_ms <= now_ts_ms < item.slot.end_ms),
        slots[0],
    )
    hourly = {}
    for item in slots:
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, dt.timezone.utc
        ).astimezone(runtime.settings.timezone)
        key = local.replace(minute=0, second=0, microsecond=0).isoformat()
        cloud = item.cloud_cover_pct
        radiation = item.shortwave_radiation_w_m2
        hourly[key] = {
            "cloud_cover": cloud,
            "shortwave_radiation": radiation,
            "weather_factor": round(weather_factor_from_cloud_rad(cloud, radiation), 4),
        }
    return {
        "cloud_cover": current.cloud_cover_pct,
        "shortwave_radiation": current.shortwave_radiation_w_m2,
        "weather_factor": round(
            weather_factor_from_cloud_rad(
                current.cloud_cover_pct, current.shortwave_radiation_w_m2
            ),
            4,
        ),
        "hourly": hourly,
    }


def build_production_forecast(
    intervals,
    targets,
    *,
    now_local,
    weather_factor,
    forecast_tomorrow_kwh,
    load_bias,
    load_bias_slots,
    pv_bias_slots,
    pv_now_actual_w,
    pv_global_bias,
    pv_capacity_kwp,
    pv_inverter_kw,
    history,
    context,
    weather,
    component_specs,
):
    """Build the sole production forecast from finalized feature history."""
    started = time.perf_counter()
    request = ForecastRequest(
        as_of_ms=int(now_local.timestamp() * 1000),
        timezone=str(getattr(now_local.tzinfo, "key", now_local.tzinfo)),
        slots=tuple(
            SlotKey(
                int(item["dt"].timestamp() * 1000),
                int(item["dt"].timestamp() * 1000) + int(SLOT_H * 3600 * 1000),
            )
            for item in intervals
        ),
    )
    immutable_history = tuple(history)
    forecast_context = context
    if not isinstance(forecast_context, LoadForecastContext):
        raise FeatureStoreForecastNotReady("missing_current_load_context")
    immutable_weather = tuple(weather)
    immutable_component_specs = tuple(component_specs)
    config = ForecastModelConfig(
        timezone=request.timezone,
        load_bias=float(load_bias),
        load_slot_biases=tuple(float(value) for value in load_bias_slots),
        pv_global_bias=float(pv_global_bias),
        pv_slot_biases=tuple(float(value) for value in pv_bias_slots),
        current_weather_factor=float(weather_factor),
        current_pv_w=(
            None if pv_now_actual_w is None else max(0.0, float(pv_now_actual_w))
        ),
        tomorrow_date=(intervals[0]["dt"].date() + dt.timedelta(days=1)).isoformat(),
        tomorrow_energy_kwh=(
            None if forecast_tomorrow_kwh is None else float(forecast_tomorrow_kwh)
        ),
        pv_capacity_kwp=float(pv_capacity_kwp),
        pv_inverter_kw=float(pv_inverter_kw),
    )
    readiness = feature_store_forecast_readiness(
        tuple(
            item for item in immutable_history if item.slot.end_ms <= request.as_of_ms
        ),
        component_specs=immutable_component_specs,
    )
    forecast = build_feature_store_forecast(
        request,
        immutable_history,
        tuple(targets),
        forecast_context,
        config,
        weather=immutable_weather,
        component_specs=immutable_component_specs,
    )
    load_w = [slot.energy.p50_kwh / SLOT_H * 1000.0 for slot in forecast.load.slots]
    pv_w = [slot.energy.p50_kwh / SLOT_H * 1000.0 for slot in forecast.pv.slots]
    if len(load_w) != len(intervals) or len(pv_w) != len(intervals):
        raise ValueError("production forecast does not match requested slot grid")
    diagnostics = {
        "source": "feature_store",
        "slot_count": len(request.slots),
        "runtime_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "model_version": f"{forecast.load.model_version}+{forecast.pv.model_version}",
        "history_slot_count": readiness.history_slots,
        "load_usable_slots": readiness.load_usable_slots,
        "pv_usable_slots": readiness.pv_usable_slots,
        "component_usable_slots": readiness.component_usable_slots,
        "history_span_days": readiness.history_span_days,
    }
    return forecast, diagnostics


def build_forecast_targets(intervals, weather_factor, weather_hourly=None):
    """Build the weather-aligned target grid at the composition boundary."""
    targets = []
    for item in intervals:
        hour_key = item["dt"].replace(minute=0, second=0, microsecond=0).isoformat()
        slot_weather_factor = float(
            (weather_hourly or {})
            .get(hour_key, {})
            .get("weather_factor", weather_factor)
        )
        targets.append(ForecastTargetInput(item["dt"], slot_weather_factor))
    return tuple(targets)

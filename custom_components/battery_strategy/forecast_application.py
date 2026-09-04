"""Forecast application boundary for a planning refresh."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .component_config import LoadComponentSpec
from .contracts import (
    ForecastBundle,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadForecastContext,
    PvPlant,
    SlotKey,
    WeatherSlot,
)
from .forecasting import (
    ConfiguredLoadForecaster,
    ConfiguredPvForecaster,
    FeatureStoreForecastNotReady,
    ForecastComposer,
    ForecastModelConfig,
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


@dataclass(frozen=True, slots=True)
class ProductionForecastConfig:
    """Application-owned per-run parity inputs absent from current contracts."""

    load_bias: float
    load_slot_biases: tuple[float, ...]
    pv_global_bias: float
    pv_slot_biases: tuple[float, ...]
    current_weather_factor: float
    current_pv_w: float | None
    tomorrow_energy_kwh: float | None


@dataclass(frozen=True, slots=True)
class ProductionForecastResult:
    """Forecast output plus non-authoritative evaluation metadata."""

    bundle: ForecastBundle
    diagnostics: dict[str, object]


class ProductionForecastModule:
    """Narrow legacy bridge for exact current production forecast behavior."""

    def forecast(
        self,
        request: ForecastRequest,
        history: tuple[HistoricalFeatureSlot, ...],
        context: LoadForecastContext,
        weather: tuple[WeatherSlot, ...],
        plant: PvPlant,
        config: ProductionForecastConfig,
        component_specs: tuple[LoadComponentSpec, ...] = (),
    ) -> ProductionForecastResult:
        started = time.perf_counter()
        eligible = tuple(
            item for item in history if item.slot.end_ms <= request.as_of_ms
        )
        readiness = feature_store_forecast_readiness(
            eligible, component_specs=component_specs
        )
        if not readiness.ready:
            raise FeatureStoreForecastNotReady(readiness.reason or "not_ready")

        tomorrow_date = (
            dt.datetime.fromtimestamp(
                request.slots[0].start_ms / 1000.0, dt.timezone.utc
            )
            .astimezone(ZoneInfo(request.timezone))
            .date()
            + dt.timedelta(days=1)
        ).isoformat()
        model_config = ForecastModelConfig(
            timezone=request.timezone,
            load_bias=float(config.load_bias),
            load_slot_biases=tuple(config.load_slot_biases),
            pv_global_bias=float(config.pv_global_bias),
            pv_slot_biases=tuple(config.pv_slot_biases),
            current_weather_factor=float(config.current_weather_factor),
            current_pv_w=(
                None
                if config.current_pv_w is None
                else max(0.0, float(config.current_pv_w))
            ),
            tomorrow_date=tomorrow_date,
            tomorrow_energy_kwh=config.tomorrow_energy_kwh,
            pv_capacity_kwp=plant.generator_kwp,
            pv_inverter_kw=plant.inverter_kw,
        )
        bundle = ForecastComposer(
            ConfiguredLoadForecaster(
                model_config.load_config(),
                config.current_weather_factor,
                component_specs,
            ),
            ConfiguredPvForecaster(
                model_config.pv_config(),
                config.current_weather_factor,
            ),
        ).compose(request, eligible, context, weather, plant)
        diagnostics = {
            "source": "feature_store",
            "slot_count": len(request.slots),
            "runtime_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "model_version": (f"{bundle.load.model_version}+{bundle.pv.model_version}"),
            "history_slot_count": readiness.history_slots,
            "load_usable_slots": readiness.load_usable_slots,
            "pv_usable_slots": readiness.pv_usable_slots,
            "component_usable_slots": readiness.component_usable_slots,
            "history_span_days": readiness.history_span_days,
        }
        return ProductionForecastResult(bundle, diagnostics)


def forecast_request(
    intervals, *, captured_at_ms: int, timezone: str
) -> ForecastRequest:
    """Create the contract request from one captured planning grid."""
    return ForecastRequest(
        as_of_ms=int(captured_at_ms),
        timezone=timezone,
        slots=tuple(
            SlotKey(
                int(item.starts_at.timestamp() * 1000),
                int(item.starts_at.timestamp() * 1000) + int(SLOT_H * 3600 * 1000),
            )
            for item in intervals
        ),
    )

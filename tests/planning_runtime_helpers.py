"""Typed planning snapshot fixtures for tests."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from custom_components.battery_strategy.models import StrategyOptions
from custom_components.battery_strategy.planning_runtime import (
    PlanningHistory,
    PlanningObservations,
    PlanningRuntime,
    PlanningRuntimeSettings,
)
from custom_components.battery_strategy.runtime_market_data import TariffSchedule


def settings_from_values(**values) -> PlanningRuntimeSettings:
    timezone = values.pop("timezone", "UTC")
    aliases = {
        "pv_inverter_kw": "pv_inverter_power_kw",
        "export_opportunity_ct_per_kwh": "feed_in_tariff_ct_per_kwh",
    }
    options_values = {
        aliases.get(key, key): value
        for key, value in values.items()
        if key != "captured_at_ms"
    }
    return PlanningRuntimeSettings.from_options(
        StrategyOptions(**options_values), ZoneInfo(str(timezone))
    )


def runtime_snapshot(
    *,
    captured_at_ms: int = 1_800_000_000_000,
    settings: PlanningRuntimeSettings | None = None,
    observations: PlanningObservations | None = None,
    history_series=None,
    provider_prices=(),
    forecast_history=(),
    forecast_weather=(),
    forecast_context=None,
    forecast_component_specs=(),
) -> PlanningRuntime:
    settings = settings or settings_from_values()
    observations = observations or PlanningObservations(
        current_price_ct_per_kwh=None,
        future_max_price_ct_per_kwh=None,
        grid_import_w=0.0,
        grid_export_w=0.0,
        pv_generation_w=0.0,
        battery_power_w=0.0,
        battery_soc_pct=None,
        battery_min_soc_pct=settings.min_soc_pct,
        ev_charge_w=0.0,
        heat_pump_power_w=0.0,
        pv_next_hour_kwh=0.0,
        pv_tomorrow_kwh=None,
        cloud_cover_pct=50.0,
        shortwave_radiation_w_m2=0.0,
    )
    return PlanningRuntime(
        captured_at_ms=captured_at_ms,
        settings=settings,
        observations=observations,
        history=PlanningHistory.from_series(
            history_series or {}, captured_at_s=captured_at_ms / 1000.0
        ),
        tariffs=TariffSchedule.from_provider_rows(
            list(provider_prices), settings.timezone
        ),
        forecast_history=tuple(forecast_history),
        forecast_weather=tuple(forecast_weather),
        forecast_context=forecast_context,
        forecast_component_specs=tuple(forecast_component_specs),
    )

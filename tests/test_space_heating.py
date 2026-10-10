from __future__ import annotations

import datetime as dt

import pytest

from custom_components.battery_strategy.contracts import (
    DataQuality,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadDriverSnapshot,
    LoadFeatureValue,
    LoadForecastContext,
    QualityFlag,
    SlotKey,
    WeatherSlot,
)
from custom_components.battery_strategy.forecasting.space_heating import (
    MODEL_VERSION,
    forecast_space_heating,
)

SLOT_MS = 15 * 60 * 1000


def _slot(start_ms: int) -> SlotKey:
    return SlotKey(start_ms, start_ms + SLOT_MS)


def _feature(key: str, value: float) -> LoadFeatureValue:
    return LoadFeatureValue(key, value)


def _history_slot(
    start_ms: int,
    heating_kwh: float,
    oat_c: float = 5.0,
    *,
    dhw_kwh: float = 0.0,
    dhw_fraction: float = 0.0,
) -> HistoricalFeatureSlot:
    return HistoricalFeatureSlot(
        slot=_slot(start_ms),
        house_load_no_ev_kwh=0.2 + heating_kwh,
        pv_generation_kwh=0.0,
        grid_import_kwh=0.2 + heating_kwh,
        grid_export_kwh=0.0,
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        ev_charge_kwh=0.0,
        price_ct_per_kwh=30.0,
        load_components=(
            LoadComponentEnergy(
                "heat_pump_dhw",
                dhw_kwh,
                features=(_feature("dhw_charging_fraction", dhw_fraction),),
            ),
            LoadComponentEnergy(
                "heat_pump_space_heating",
                heating_kwh,
                features=(
                    _feature("outdoor_temperature_c", oat_c),
                    _feature("target_flow_temperature_c", 32.0),
                    _feature("heating_active_fraction", 1.0 if heating_kwh else 0.0),
                ),
            ),
        ),
    )


def _case(*, active: bool, dhw=(0.0, 0.0, 0.0, 0.0)):
    as_of = int(dt.datetime(2026, 10, 20, 6, 0, tzinfo=dt.UTC).timestamp() * 1000)
    slots = tuple(_slot(as_of + index * SLOT_MS) for index in range(4))
    request = ForecastRequest(as_of, "Europe/Berlin", slots)
    history = tuple(
        _history_slot(as_of - (96 - index) * SLOT_MS, 0.0) for index in range(96)
    )
    context = LoadForecastContext(
        1800.0 if active else 200.0,
        (
            LoadDriverSnapshot("heat_pump_dhw", 0.0),
            LoadDriverSnapshot(
                "heat_pump_space_heating",
                1600.0 if active else 0.0,
                features=(
                    _feature("outdoor_temperature_c", 5.0),
                    _feature("target_flow_temperature_c", 32.0),
                    _feature("heating_active_fraction", 1.0 if active else 0.0),
                    _feature("heating_active_age_s", 15 * 60.0 if active else 0.0),
                ),
            ),
        ),
    )
    weather = tuple(WeatherSlot(slot, 5.0, None, None, DataQuality()) for slot in slots)
    return request, history, context, weather, dhw


def test_active_space_heating_is_visible_despite_zero_historical_slot_baseline():
    request, history, context, weather, dhw = _case(active=True)

    result = forecast_space_heating(request, history, context, weather, dhw)

    assert result.model_version == MODEL_VERSION
    assert result.energy_kwh[0] > 0.2
    assert result.energy_kwh[-1] < result.energy_kwh[0]


def test_dhw_occupancy_defers_space_heating_instead_of_double_counting():
    request, history, context, weather, dhw = _case(
        active=True, dhw=(0.4, 0.0, 0.0, 0.0)
    )

    result = forecast_space_heating(request, history, context, weather, dhw)

    assert result.energy_kwh[0] == 0.0
    assert result.energy_kwh[1] > 0.0


def test_historical_dhw_occupancy_is_not_displaced_twice():
    request, _, context, weather, dhw = _case(active=False, dhw=(0.2, 0.0, 0.0, 0.0))
    history = tuple(
        _history_slot(
            request.as_of_ms - (96 - index) * SLOT_MS,
            0.2,
            dhw_kwh=0.2,
            dhw_fraction=0.5,
        )
        for index in range(96)
    )

    result = forecast_space_heating(request, history, context, weather, dhw)

    assert result.energy_kwh[0] == pytest.approx(0.2)
    assert result.energy_kwh[1] == pytest.approx(0.2)


def test_active_run_respects_shared_compressor_occupancy():
    request, _, context, weather, dhw = _case(active=True, dhw=(0.2, 0.0, 0.0, 0.0))
    history = tuple(
        _history_slot(
            request.as_of_ms - (96 - index) * SLOT_MS,
            0.2,
            dhw_kwh=0.2,
            dhw_fraction=0.5,
        )
        for index in range(96)
    )

    result = forecast_space_heating(request, history, context, weather, dhw)

    assert result.energy_kwh[0] == pytest.approx(0.2)


def test_missing_dhw_active_power_is_explicitly_estimated():
    request, _, context, weather, dhw = _case(active=True, dhw=(0.01, 0.0, 0.0, 0.0))
    history = tuple(
        _history_slot(
            request.as_of_ms - (96 - index) * SLOT_MS,
            0.0,
            dhw_fraction=0.5,
        )
        for index in range(96)
    )

    result = forecast_space_heating(request, history, context, weather, dhw)

    assert QualityFlag.ESTIMATED in result.quality[0].flags
    assert sum(QualityFlag.ESTIMATED in item.flags for item in result.quality) == 1


def test_open_historical_run_is_right_censored_not_treated_as_completed():
    request, _, _, weather, dhw = _case(active=True)
    history = tuple(
        _history_slot(
            request.as_of_ms - (96 - index) * SLOT_MS,
            0.4 if index >= 94 else 0.0,
        )
        for index in range(96)
    )
    context = LoadForecastContext(
        1800.0,
        (
            LoadDriverSnapshot("heat_pump_dhw", 0.0),
            LoadDriverSnapshot(
                "heat_pump_space_heating",
                1600.0,
                features=(
                    _feature("outdoor_temperature_c", 5.0),
                    _feature("target_flow_temperature_c", 32.0),
                    _feature("heating_active_fraction", 1.0),
                    _feature("heating_active_age_s", 30 * 60.0),
                ),
            ),
        ),
    )

    result = forecast_space_heating(request, history, context, weather, dhw)

    assert result.energy_kwh[0] > 0.2


def test_cold_start_is_explicit_and_zero():
    request, _, context, weather, dhw = _case(active=False)

    result = forecast_space_heating(request, (), context, weather, dhw)

    assert result.energy_kwh == (0.0, 0.0, 0.0, 0.0)
    assert all(item.coverage == 0.0 for item in result.quality)
    assert all(QualityFlag.ESTIMATED in item.flags for item in result.quality)


def test_active_cold_start_preserves_measured_live_power():
    request, _, context, weather, dhw = _case(active=True)

    result = forecast_space_heating(request, (), context, weather, dhw)

    assert result.energy_kwh[0] > 0.2
    assert result.energy_kwh[-1] < result.energy_kwh[0]
    assert all(QualityFlag.ESTIMATED in item.flags for item in result.quality)

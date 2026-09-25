from __future__ import annotations

import datetime as dt

import pytest

from custom_components.battery_strategy.contracts import (
    DataQuality,
    ForecastRequest,
    ForecastSlot,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadDriverSnapshot,
    LoadFeatureValue,
    LoadForecast,
    LoadForecastComponent,
    LoadForecastContext,
    QualityFlag,
    QuantileEnergy,
    SlotKey,
    WeatherSlot,
)
from custom_components.battery_strategy.forecasting.heat_pump_shadow import (
    build_heat_pump_shadow_forecast,
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
):
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


def _authoritative_load(
    request: ForecastRequest,
    dhw=(0.0, 0.0, 0.0, 0.0),
    *,
    dhw_quantiles: bool = False,
):
    zero = tuple(ForecastSlot(slot, QuantileEnergy(0.0)) for slot in request.slots)
    dhw_slots = tuple(
        ForecastSlot(
            slot,
            QuantileEnergy(value, max(0.0, value - 0.1), value + 0.1, 20)
            if dhw_quantiles
            else QuantileEnergy(value),
        )
        for slot, value in zip(request.slots, dhw, strict=True)
    )
    components = (
        LoadForecastComponent("general_house_load", "general-v1", 0, zero),
        LoadForecastComponent("heat_pump_dhw", "dhw-v1", 0, dhw_slots),
        LoadForecastComponent("heat_pump_space_heating", "heating-v1", 0, zero),
    )
    total = tuple(
        ForecastSlot(slot, QuantileEnergy(dhw[index]))
        for index, slot in enumerate(request.slots)
    )
    return LoadForecast("load", request.as_of_ms, 0, "load-v1", total, components)


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
    return request, history, context, weather, _authoritative_load(request, dhw)


def test_active_space_heating_is_visible_despite_zero_historical_slot_baseline():
    request, history, context, weather, authoritative = _case(active=True)

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    heating = result.component("heat_pump_space_heating")
    assert heating.slots[0].energy.p50_kwh > 0.2
    assert heating.slots[0].energy.p10_kwh is None
    assert heating.slots[0].energy.p90_kwh is None
    assert heating.slots[0].energy.calibration_samples == 0
    assert heating.slots[-1].energy.p50_kwh < heating.slots[0].energy.p50_kwh
    assert authoritative.components[2].slots[0].energy.p50_kwh == 0.0


def test_dhw_occupancy_defers_space_heating_instead_of_double_counting():
    request, history, context, weather, authoritative = _case(
        active=True, dhw=(0.4, 0.0, 0.0, 0.0)
    )

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    heating = result.component("heat_pump_space_heating")
    assert heating.slots[0].energy.p50_kwh == 0.0
    assert heating.slots[1].energy.p50_kwh > 0.0
    assert result.total_slots[0].energy.p50_kwh == 0.4


def test_historical_dhw_occupancy_is_not_displaced_twice():
    request, _, context, weather, authoritative = _case(
        active=False, dhw=(0.2, 0.0, 0.0, 0.0)
    )
    history = tuple(
        _history_slot(
            request.as_of_ms - (96 - index) * SLOT_MS,
            0.2,
            dhw_kwh=0.2,
            dhw_fraction=0.5,
        )
        for index in range(96)
    )

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    heating = result.component("heat_pump_space_heating")
    assert heating.slots[0].energy.p50_kwh == pytest.approx(0.2)
    assert heating.slots[1].energy.p50_kwh == pytest.approx(0.2)


def test_active_run_respects_shared_compressor_occupancy():
    request, _, context, weather, authoritative = _case(
        active=True, dhw=(0.2, 0.0, 0.0, 0.0)
    )
    history = tuple(
        _history_slot(
            request.as_of_ms - (96 - index) * SLOT_MS,
            0.2,
            dhw_kwh=0.2,
            dhw_fraction=0.5,
        )
        for index in range(96)
    )

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    assert result.component("heat_pump_space_heating").slots[0].energy.p50_kwh == (
        pytest.approx(0.2)
    )
    assert result.total_slots[0].energy.p50_kwh == pytest.approx(0.4)


def test_missing_dhw_active_power_is_explicitly_estimated():
    request, _, context, weather, authoritative = _case(
        active=True, dhw=(0.01, 0.0, 0.0, 0.0)
    )
    history = tuple(
        _history_slot(
            request.as_of_ms - (96 - index) * SLOT_MS,
            0.0,
            dhw_kwh=0.0,
            dhw_fraction=0.5,
        )
        for index in range(96)
    )

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    heating = result.component("heat_pump_space_heating")
    assert QualityFlag.ESTIMATED in heating.slots[0].quality.flags
    assert result.diagnostics["missing_dhw_capacity_slots"] == 1


def test_open_historical_run_is_right_censored_not_treated_as_completed():
    request, _, _, weather, authoritative = _case(active=True)
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

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    assert result.component("heat_pump_space_heating").slots[0].energy.p50_kwh > 0.2


def test_shadow_training_cutoff_never_exceeds_generation_time():
    request, history, context, weather, authoritative = _case(active=False)

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    assert result.training_cutoff_ms <= request.as_of_ms
    assert result.non_authoritative is True


def test_shadow_strips_authoritative_dhw_quantiles_until_candidate_is_calibrated():
    request, history, context, weather, _ = _case(active=False)
    authoritative = _authoritative_load(
        request, (0.2, 0.0, 0.0, 0.0), dhw_quantiles=True
    )

    result = build_heat_pump_shadow_forecast(
        request, history, context, weather, authoritative
    )

    dhw = result.component("heat_pump_dhw")
    assert all(slot.energy.p10_kwh is None for slot in dhw.slots)
    assert all(slot.energy.p90_kwh is None for slot in dhw.slots)
    assert all(slot.energy.calibration_samples == 0 for slot in dhw.slots)

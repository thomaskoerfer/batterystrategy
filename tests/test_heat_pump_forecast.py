"""Regression tests for finite DHW cycles and heating recovery coupling."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

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
)
from custom_components.battery_strategy.forecasting.heat_pump import (
    _couple_space_heating,
    adjust_heat_pump_forecast,
)
from custom_components.battery_strategy.forecasting.history import ForecastTargetInput

SLOT_MS = 900_000
TZ = ZoneInfo("Europe/Berlin")


def _slot(local: dt.datetime) -> SlotKey:
    start_ms = int(local.astimezone(dt.UTC).timestamp() * 1000)
    return SlotKey(start_ms, start_ms + SLOT_MS)


def _feature(key: str, value: float) -> LoadFeatureValue:
    return LoadFeatureValue(key, value)


def _history_slot(
    local: dt.datetime,
    *,
    dhw_kwh: float,
    dhw_temperature_c: float,
    charging_fraction: float | None,
    heating_kwh: float = 0.0,
    outdoor_temperature_c: float = 10.0,
    quality: DataQuality = DataQuality(),
) -> HistoricalFeatureSlot:
    features = [
        _feature("dhw_temperature_c", dhw_temperature_c),
        _feature("dhw_target_c", 53.0),
        _feature("dhw_differential_c", 9.0),
        _feature("outdoor_temperature_c", outdoor_temperature_c),
    ]
    if charging_fraction is not None:
        features.append(_feature("dhw_charging_fraction", charging_fraction))
    return HistoricalFeatureSlot(
        slot=_slot(local),
        house_load_no_ev_kwh=dhw_kwh + heating_kwh + 0.1,
        pv_generation_kwh=0.0,
        grid_import_kwh=dhw_kwh + heating_kwh + 0.1,
        grid_export_kwh=0.0,
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        ev_charge_kwh=0.0,
        price_ct_per_kwh=30.0,
        load_components=(
            LoadComponentEnergy(
                "heat_pump_dhw",
                dhw_kwh,
                quality=quality,
                features=tuple(features),
            ),
            LoadComponentEnergy("heat_pump_space_heating", heating_kwh),
        ),
    )


def _completed_cycle(
    day: int,
    *,
    start_temperature_c: float = 43.0,
    outdoor_temperature_c: float = 10.0,
    energy_scale: float = 1.0,
    include_charging_feature: bool = True,
    quality: DataQuality = DataQuality(),
    heating_kwh: float = 0.0,
) -> tuple[HistoricalFeatureSlot, ...]:
    base = dt.datetime(2026, 8, day, 3, 0, tzinfo=TZ)
    return (
        _history_slot(
            base,
            dhw_kwh=0.60 * energy_scale,
            dhw_temperature_c=start_temperature_c,
            charging_fraction=1.0 if include_charging_feature else None,
            outdoor_temperature_c=outdoor_temperature_c,
            quality=quality,
            heating_kwh=heating_kwh,
        ),
        _history_slot(
            base + dt.timedelta(minutes=15),
            dhw_kwh=0.75 * energy_scale,
            dhw_temperature_c=48.0,
            charging_fraction=1.0 if include_charging_feature else None,
            outdoor_temperature_c=outdoor_temperature_c,
            quality=quality,
            heating_kwh=heating_kwh,
        ),
        _history_slot(
            base + dt.timedelta(minutes=30),
            dhw_kwh=0.30 * energy_scale,
            dhw_temperature_c=54.0,
            charging_fraction=0.4 if include_charging_feature else None,
            outdoor_temperature_c=outdoor_temperature_c,
            quality=quality,
            heating_kwh=heating_kwh,
        ),
        _history_slot(
            base + dt.timedelta(minutes=45),
            dhw_kwh=0.0,
            dhw_temperature_c=54.0,
            charging_fraction=0.0 if include_charging_feature else None,
            outdoor_temperature_c=outdoor_temperature_c,
            quality=quality,
            heating_kwh=heating_kwh,
        ),
    )


def _case(
    temperature_c: float,
    *,
    as_of_minute: int = 30,
    outdoor_temperature_c: float = 10.0,
    history_prefix: tuple[HistoricalFeatureSlot, ...] = (),
    dhw_baseline: tuple[float, ...] | None = None,
    heating_baseline: tuple[float, ...] = (0.0, 0.0, 0.0),
    include_default_cycles: bool = True,
    current_power_w: float = 4000.0,
    current_charging_fraction: float = 1.0,
):
    current = dt.datetime(2026, 9, 6, 3, as_of_minute, tzinfo=TZ)
    slot_start = current.replace(minute=30)
    slots = tuple(
        _slot(slot_start + dt.timedelta(minutes=15 * i))
        for i in range(len(heating_baseline))
    )
    request = ForecastRequest(
        int(current.astimezone(dt.UTC).timestamp() * 1000),
        "Europe/Berlin",
        slots,
    )
    targets = tuple(
        ForecastTargetInput(
            dt.datetime.fromtimestamp(slot.start_ms / 1000.0, dt.UTC).astimezone(TZ),
            1.0,
        )
        for slot in slots
    )
    history = (
        tuple(item for day in range(23, 27) for item in _completed_cycle(day))
        if include_default_cycles
        else ()
    ) + history_prefix
    context = LoadForecastContext(
        4200.0,
        (
            LoadDriverSnapshot(
                "heat_pump_dhw",
                current_power_w,
                features=(
                    _feature("dhw_temperature_c", temperature_c),
                    _feature("dhw_target_c", 53.0),
                    _feature("dhw_differential_c", 9.0),
                    _feature("dhw_charging_fraction", current_charging_fraction),
                    _feature("outdoor_temperature_c", outdoor_temperature_c),
                ),
            ),
            LoadDriverSnapshot("heat_pump_space_heating", 0.0),
        ),
    )
    return adjust_heat_pump_forecast(
        request,
        history,
        targets,
        context,
        "03:00-05:00,09:00-17:00",
        dhw_baseline or (0.30, *(0.0 for _ in range(len(heating_baseline) - 1))),
        heating_baseline,
    )


def test_active_cycle_uses_remaining_delta_t_instead_of_rolling_45_minutes():
    observed = (
        _history_slot(
            dt.datetime(2026, 9, 6, 3, 0, tzinfo=TZ),
            dhw_kwh=0.60,
            dhw_temperature_c=43.0,
            charging_fraction=1.0,
        ),
        _history_slot(
            dt.datetime(2026, 9, 6, 3, 15, tzinfo=TZ),
            dhw_kwh=0.75,
            dhw_temperature_c=48.0,
            charging_fraction=1.0,
        ),
    )
    forecast = _case(
        51.0,
        history_prefix=observed,
        dhw_baseline=(0.30, 0.25, 0.20),
    )

    assert 0.25 < forecast.dhw_kwh[0] < 0.60
    assert forecast.dhw_kwh[1:] == (0.0, 0.0)


def test_unchanged_active_snapshot_does_not_move_cycle_end_during_slot():
    observed = (
        _history_slot(
            dt.datetime(2026, 9, 6, 3, 0, tzinfo=TZ),
            dhw_kwh=0.60,
            dhw_temperature_c=43.0,
            charging_fraction=1.0,
        ),
        _history_slot(
            dt.datetime(2026, 9, 6, 3, 15, tzinfo=TZ),
            dhw_kwh=0.75,
            dhw_temperature_c=48.0,
            charging_fraction=1.0,
        ),
    )

    at_slot_start = _case(49.0, as_of_minute=30, history_prefix=observed)
    twelve_minutes_later = _case(49.0, as_of_minute=42, history_prefix=observed)

    assert sum(twelve_minutes_later.dhw_kwh) < sum(at_slot_start.dhw_kwh)
    assert at_slot_start.dhw_kwh[1:] == (0.0, 0.0)
    assert twelve_minutes_later.dhw_kwh[1:] == (0.0, 0.0)


def test_cycle_starting_in_current_slot_never_creates_rolling_tail():
    at_slot_start = _case(49.0, as_of_minute=30)
    four_minutes_later = _case(49.0, as_of_minute=34)
    twelve_minutes_later = _case(49.0, as_of_minute=42)

    assert at_slot_start.dhw_kwh[1:] == (0.0, 0.0)
    assert four_minutes_later.dhw_kwh[1:] == (0.0, 0.0)
    assert twelve_minutes_later.dhw_kwh[1:] == (0.0, 0.0)
    assert at_slot_start.dhw_kwh[0] > four_minutes_later.dhw_kwh[0]
    assert four_minutes_later.dhw_kwh[0] > twelve_minutes_later.dhw_kwh[0]


def test_smaller_remaining_delta_t_reduces_remaining_energy():
    cooler = _case(49.0)
    warmer = _case(51.0)

    assert sum(cooler.dhw_kwh) > sum(warmer.dhw_kwh)


def test_outdoor_temperature_selects_comparable_cycle_efficiency():
    warm_cycles = tuple(
        item
        for day in range(23, 27)
        for item in _completed_cycle(
            day,
            outdoor_temperature_c=20.0,
            energy_scale=0.75,
        )
    )
    cold_cycles = tuple(
        item
        for day in range(27, 31)
        for item in _completed_cycle(
            day,
            outdoor_temperature_c=-5.0,
            energy_scale=1.35,
        )
    )
    history = warm_cycles + cold_cycles

    warm = _case(49.0, outdoor_temperature_c=20.0, history_prefix=history)
    cold = _case(49.0, outdoor_temperature_c=-5.0, history_prefix=history)

    assert sum(cold.dhw_kwh) > sum(warm.dhw_kwh)


def test_optional_charging_sensor_is_not_required_for_cycle_learning():
    cycles = tuple(
        item
        for day in range(23, 27)
        for item in _completed_cycle(day, include_charging_feature=False)
    )

    forecast = _case(
        44.0,
        history_prefix=cycles,
        include_default_cycles=False,
        current_power_w=0.0,
        current_charging_fraction=0.0,
    )

    assert forecast.dhw_kwh[1] > 0.0


def test_estimated_cycle_evidence_is_not_used_for_delta_t_calibration():
    unreliable = tuple(
        item
        for day in range(27, 31)
        for item in _completed_cycle(
            day,
            energy_scale=5.0,
            quality=DataQuality(1.0, (QualityFlag.ESTIMATED,)),
        )
    )

    reference = _case(49.0)
    with_unreliable_history = _case(49.0, history_prefix=unreliable)

    assert with_unreliable_history.dhw_kwh == pytest.approx(reference.dhw_kwh)


def test_shorter_dhw_cycle_restores_heating_and_avoids_duplicate_recovery():
    winter_cycles = tuple(
        item
        for day in range(23, 27)
        for item in _completed_cycle(day, heating_kwh=0.40)
    )
    forecast = _case(
        54.0,
        history_prefix=winter_cycles,
        include_default_cycles=False,
        dhw_baseline=(0.30, 0.0, 0.0),
        heating_baseline=(0.20, 0.20, 0.20),
    )

    assert forecast.space_heating_kwh[0] > 0.20
    assert forecast.space_heating_kwh[1] < 0.20
    assert sum(forecast.space_heating_kwh) == pytest.approx(0.60)


def test_longer_dhw_cycle_defers_space_heating_to_following_slot():
    forecast = _case(
        44.0,
        heating_baseline=(0.4, 0.2, 0.1, 0.1, 0.1, 0.1),
    )

    assert forecast.space_heating_kwh[0] < 0.4
    assert forecast.space_heating_kwh[2] > 0.1
    assert sum(forecast.space_heating_kwh) == pytest.approx(1.0)


def test_unchanged_dhw_occupancy_keeps_heating_baseline_exactly():
    context = LoadForecastContext(
        1600.0,
        (LoadDriverSnapshot("heat_pump_space_heating", 1600.0),),
    )
    baseline = (0.20, 0.20, 0.20)
    occupancy = (0.50, 0.0, 0.0)

    result = _couple_space_heating((), context, occupancy, occupancy, baseline)

    assert result == baseline

"""Regression tests for finite DHW cycles and heating recovery coupling."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
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
from custom_components.battery_strategy.feature_store import CompressedFeatureStore
from custom_components.battery_strategy.forecasting.dhw_timing import (
    estimate_next_cycle_start,
)
from custom_components.battery_strategy.forecasting.heat_pump import (
    _couple_space_heating,
    adjust_heat_pump_forecast,
)
from custom_components.battery_strategy.forecasting.history import ForecastTargetInput

SLOT_MS = 900_000
TZ = ZoneInfo("Europe/Berlin")


def _last_active_slot(values: tuple[float, ...]) -> int | None:
    return next(
        (index for index in range(len(values) - 1, -1, -1) if values[index] > 0),
        None,
    )


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
    target_temperature_c: float = 53.0,
    quality: DataQuality = DataQuality(),
) -> HistoricalFeatureSlot:
    features = [
        _feature("dhw_temperature_c", dhw_temperature_c),
        _feature("dhw_target_c", target_temperature_c),
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
    target_temperature_c: float = 53.0,
    peak_temperature_c: float = 54.0,
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
            target_temperature_c=target_temperature_c,
        ),
        _history_slot(
            base + dt.timedelta(minutes=15),
            dhw_kwh=0.75 * energy_scale,
            dhw_temperature_c=48.0,
            charging_fraction=1.0 if include_charging_feature else None,
            outdoor_temperature_c=outdoor_temperature_c,
            quality=quality,
            heating_kwh=heating_kwh,
            target_temperature_c=target_temperature_c,
        ),
        _history_slot(
            base + dt.timedelta(minutes=30),
            dhw_kwh=0.30 * energy_scale,
            dhw_temperature_c=peak_temperature_c,
            charging_fraction=0.4 if include_charging_feature else None,
            outdoor_temperature_c=outdoor_temperature_c,
            quality=quality,
            heating_kwh=heating_kwh,
            target_temperature_c=target_temperature_c,
        ),
        _history_slot(
            base + dt.timedelta(minutes=45),
            dhw_kwh=0.0,
            dhw_temperature_c=peak_temperature_c,
            charging_fraction=0.0 if include_charging_feature else None,
            outdoor_temperature_c=outdoor_temperature_c,
            quality=quality,
            heating_kwh=heating_kwh,
            target_temperature_c=target_temperature_c,
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
    current_charging_age_s: float | None = None,
):
    current = dt.datetime(2026, 9, 6, 3, as_of_minute, tzinfo=TZ)
    slot_start = current.replace(minute=as_of_minute // 15 * 15)
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
                    _feature(
                        "dhw_active_age_s",
                        current_charging_age_s
                        if current_charging_age_s is not None
                        else max(0, as_of_minute - 30) * 60,
                    ),
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


def _with_dhw_feature(
    slot: HistoricalFeatureSlot, key: str, value: float
) -> HistoricalFeatureSlot:
    dhw = slot.load_components[0]
    return replace(
        slot,
        load_components=(
            replace(dhw, features=(*dhw.features, _feature(key, value))),
            *slot.load_components[1:],
        ),
    )


def _future_inactive_case(
    *,
    current: dt.datetime,
    temperature_c: float,
    history: tuple[HistoricalFeatureSlot, ...],
    dhw_baseline: tuple[float, ...],
    allowed_windows: str = "03:00-05:00,09:00-17:00",
    circulation_fraction: float | None = 0.0,
):
    slots = tuple(
        _slot(current + dt.timedelta(minutes=15 * index))
        for index in range(len(dhw_baseline))
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
    features = [
        _feature("dhw_temperature_c", temperature_c),
        _feature("dhw_target_c", 53.0),
        _feature("dhw_differential_c", 9.0),
        _feature("dhw_charging_fraction", 0.0),
        _feature("outdoor_temperature_c", 10.0),
    ]
    if circulation_fraction is not None:
        features.append(_feature("circulation_fraction", circulation_fraction))
    context = LoadForecastContext(
        0.0,
        (
            LoadDriverSnapshot(
                "heat_pump_dhw",
                0.0,
                features=tuple(features),
            ),
            LoadDriverSnapshot("heat_pump_space_heating", 0.0),
        ),
    )
    return adjust_heat_pump_forecast(
        request,
        history,
        targets,
        context,
        allowed_windows,
        dhw_baseline,
        tuple(0.0 for _ in dhw_baseline),
    )


def _temperature_timing_history() -> tuple[HistoricalFeatureSlot, ...]:
    history = []
    for day in (
        dt.date(2026, 8, 16),
        dt.date(2026, 8, 23),
        dt.date(2026, 8, 30),
    ):
        for index in range(11):
            history.append(
                _with_dhw_feature(
                    _history_slot(
                        dt.datetime.combine(day, dt.time(9, 45), tzinfo=TZ)
                        + dt.timedelta(minutes=15 * index),
                        dhw_kwh=0.0,
                        dhw_temperature_c=46.2 - 0.2 * index,
                        charging_fraction=0.0,
                    ),
                    "circulation_fraction",
                    0.0,
                )
            )
        cycle_start = dt.datetime.combine(day, dt.time(12, 30), tzinfo=TZ)
        history.extend(
            (
                _history_slot(
                    cycle_start,
                    dhw_kwh=0.60,
                    dhw_temperature_c=44.0,
                    charging_fraction=1.0,
                ),
                _history_slot(
                    cycle_start + dt.timedelta(minutes=15),
                    dhw_kwh=0.75,
                    dhw_temperature_c=49.0,
                    charging_fraction=1.0,
                ),
                _history_slot(
                    cycle_start + dt.timedelta(minutes=30),
                    dhw_kwh=0.30,
                    dhw_temperature_c=54.0,
                    charging_fraction=0.4,
                ),
            )
        )
    return tuple(sorted(history, key=lambda item: item.slot.start_ms))


def test_inactive_tank_temperature_projects_future_hysteresis_crossing():
    baseline = tuple(0.3 if 8 <= index <= 10 else 0.0 for index in range(20))

    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=_temperature_timing_history(),
        dhw_baseline=baseline,
    )

    assert forecast.dhw_kwh[8] == 0.0
    assert forecast.dhw_kwh[10] > 0.0


def test_one_slot_temperature_timing_difference_keeps_historical_prior():
    baseline = tuple(0.3 if 9 <= index <= 11 else 0.0 for index in range(20))

    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=_temperature_timing_history(),
        dhw_baseline=baseline,
    )

    assert forecast.dhw_kwh == baseline


def test_history_gap_does_not_create_temperature_timing_evidence():
    inactive = _with_dhw_feature(
        _history_slot(
            dt.datetime(2026, 8, 30, 10, 0, tzinfo=TZ),
            dhw_kwh=0.0,
            dhw_temperature_c=46.2,
            charging_fraction=0.0,
        ),
        "circulation_fraction",
        0.0,
    )
    cycle = _completed_cycle(31)
    baseline = tuple(0.3 if 6 <= index <= 8 else 0.0 for index in range(20))

    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=(inactive, *cycle),
        dhw_baseline=baseline,
    )

    assert forecast.dhw_kwh == baseline


def test_single_historical_cycle_cannot_move_timing_prior():
    baseline = tuple(0.3 if 6 <= index <= 8 else 0.0 for index in range(20))

    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=_temperature_timing_history()[:14],
        dhw_baseline=baseline,
    )

    assert forecast.dhw_kwh == baseline


def test_temperature_timing_cannot_start_outside_allowed_window():
    baseline = tuple(0.3 if 2 <= index <= 4 else 0.0 for index in range(20))

    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=_temperature_timing_history(),
        dhw_baseline=baseline,
        allowed_windows="09:00-11:00",
    )

    assert forecast.dhw_kwh == baseline


def test_temperature_timing_does_not_overwrite_following_cycle():
    baseline = tuple(
        0.3 if 6 <= index <= 8 or 10 <= index <= 12 else 0.0 for index in range(20)
    )

    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=_temperature_timing_history(),
        dhw_baseline=baseline,
    )

    assert forecast.dhw_kwh == baseline


def test_temperature_timing_beyond_horizon_is_not_clamped_to_last_slot():
    current = dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ)
    slots = tuple(
        _slot(current + dt.timedelta(minutes=15 * index)) for index in range(4)
    )
    request = ForecastRequest(
        int(current.astimezone(dt.UTC).timestamp() * 1000),
        "Europe/Berlin",
        slots,
    )

    assert (
        estimate_next_cycle_start(
            request,
            _temperature_timing_history(),
            46.2,
            0.0,
        )
        is None
    )


def test_missing_current_circulation_does_not_mean_circulation_off():
    history = _temperature_timing_history()
    baseline = tuple(0.3 if 6 <= index <= 8 else 0.0 for index in range(20))

    without_circulation = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=history,
        dhw_baseline=baseline,
        circulation_fraction=None,
    )
    with_circulation_off = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.2,
        history=history,
        dhw_baseline=baseline,
        circulation_fraction=0.0,
    )

    assert without_circulation == with_circulation_off


def test_circulation_state_selects_matching_time_to_next_cycle():
    history = []
    first_sunday = dt.date(2026, 6, 14)
    for index in range(12):
        day = first_sunday + dt.timedelta(days=7 * index)
        circulation = 1.0 if index % 2 == 0 else 0.0
        cycle_hour = 12 if circulation else 13
        inactive_slots = (cycle_hour * 60 - (9 * 60 + 45)) // 15
        for slot_index in range(inactive_slots):
            history.append(
                _with_dhw_feature(
                    _history_slot(
                        dt.datetime.combine(day, dt.time(9, 45), tzinfo=TZ)
                        + dt.timedelta(minutes=15 * slot_index),
                        dhw_kwh=0.0,
                        dhw_temperature_c=46.0,
                        charging_fraction=0.0,
                    ),
                    "circulation_fraction",
                    circulation,
                )
            )
        cycle_start = dt.datetime.combine(
            day,
            dt.time(cycle_hour, 0),
            tzinfo=TZ,
        )
        history.extend(
            (
                _history_slot(
                    cycle_start,
                    dhw_kwh=0.65,
                    dhw_temperature_c=44.0,
                    charging_fraction=1.0,
                ),
                _history_slot(
                    cycle_start + dt.timedelta(minutes=15),
                    dhw_kwh=0.65,
                    dhw_temperature_c=49.0,
                    charging_fraction=1.0,
                ),
                _history_slot(
                    cycle_start + dt.timedelta(minutes=30),
                    dhw_kwh=0.30,
                    dhw_temperature_c=54.0,
                    charging_fraction=0.4,
                ),
            )
        )
    ordered = tuple(sorted(history, key=lambda item: item.slot.start_ms))
    baseline = tuple(0.3 if 8 <= index <= 12 else 0.0 for index in range(20))

    circulation_on = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.0,
        history=ordered,
        dhw_baseline=baseline,
        circulation_fraction=1.0,
    )
    circulation_off = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 10, 0, tzinfo=TZ),
        temperature_c=46.0,
        history=ordered,
        dhw_baseline=baseline,
        circulation_fraction=0.0,
    )

    assert next(i for i, value in enumerate(circulation_on.dhw_kwh) if value) < next(
        i for i, value in enumerate(circulation_off.dhw_kwh) if value
    )


def test_hysteresis_crossing_in_blocked_period_waits_for_allowed_window():
    history = tuple(item for day in range(23, 27) for item in _completed_cycle(day))
    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 17, 0, tzinfo=TZ),
        temperature_c=43.5,
        history=history,
        dhw_baseline=tuple(0.3 if 40 <= index <= 42 else 0.0 for index in range(44)),
    )

    assert all(value == 0.0 for value in forecast.dhw_kwh[:40])
    assert forecast.dhw_kwh[40] > 0.0


def test_hysteresis_crossing_replaces_later_historical_cycle():
    history = tuple(item for day in range(23, 27) for item in _completed_cycle(day))
    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 17, 0, tzinfo=TZ),
        temperature_c=43.5,
        history=history,
        dhw_baseline=tuple(0.3 if 42 <= index <= 44 else 0.0 for index in range(48)),
    )

    assert forecast.dhw_kwh[40] > 0.0
    assert forecast.dhw_kwh[44] == 0.0


def test_hysteresis_crossing_does_not_partially_overwrite_following_cycle():
    history = tuple(
        replace(
            item,
            slot=SlotKey(
                item.slot.start_ms + 6 * 3_600_000,
                item.slot.end_ms + 6 * 3_600_000,
            ),
        )
        for day in range(23, 27)
        for item in _completed_cycle(day)
    )
    baseline = tuple(0.3 if index in {40, 42} else 0.0 for index in range(48))

    forecast = _future_inactive_case(
        current=dt.datetime(2026, 9, 6, 17, 0, tzinfo=TZ),
        temperature_c=43.5,
        history=history,
        dhw_baseline=baseline,
    )

    assert forecast.dhw_kwh == baseline


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


def test_active_cycle_removes_every_baseline_cycle_it_touches():
    forecast = _case(
        43.0,
        current_power_w=3000.0,
        dhw_baseline=(0.3, 0.0, 0.3, 0.3, 0.3, 0.0),
        heating_baseline=(0.0,) * 6,
    )

    assert forecast.dhw_kwh[2] > 0.0
    assert forecast.dhw_kwh[3:5] == (0.0, 0.0)


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


def test_cycle_starting_in_current_slot_forecasts_tail_without_rolling():
    at_slot_start = _case(44.0, as_of_minute=30)
    four_minutes_later = _case(44.0, as_of_minute=34)
    twelve_minutes_later = _case(44.0, as_of_minute=42)

    assert at_slot_start.dhw_kwh[1] > 0.0
    assert four_minutes_later.dhw_kwh[1] > 0.0
    assert twelve_minutes_later.dhw_kwh[1] > 0.0
    assert sum(at_slot_start.dhw_kwh) > sum(four_minutes_later.dhw_kwh)
    assert sum(four_minutes_later.dhw_kwh) > sum(twelve_minutes_later.dhw_kwh)
    assert _last_active_slot(at_slot_start.dhw_kwh) == _last_active_slot(
        four_minutes_later.dhw_kwh
    )
    assert _last_active_slot(four_minutes_later.dhw_kwh) == _last_active_slot(
        twelve_minutes_later.dhw_kwh
    )


def test_cycle_starting_near_boundary_uses_learned_power_during_live_ramp():
    forecast = _case(
        42.1,
        as_of_minute=30,
        current_power_w=1360.0,
        heating_baseline=(0.0, 0.0, 0.0, 0.0),
    )

    current_slot_capacity = 1360.0 * 0.25 / 1000.0
    assert forecast.dhw_kwh[0] > current_slot_capacity
    assert forecast.dhw_kwh[1] > 0.0
    assert forecast.dhw_kwh[2] > 0.0
    assert forecast.dhw_kwh[3] == 0.0


def test_cycle_starting_late_in_slot_only_deducts_actual_active_age():
    late_start = _case(
        44.0,
        as_of_minute=42,
        current_power_w=1000.0,
        current_charging_age_s=60.0,
    )
    incorrectly_backdated = _case(
        44.0,
        as_of_minute=42,
        current_power_w=1000.0,
        current_charging_age_s=12 * 60.0,
    )

    assert sum(late_start.dhw_kwh) > sum(incorrectly_backdated.dhw_kwh) + 0.15


def test_active_cycle_tail_does_not_roll_at_slot_boundary_before_store_upsert():
    before_boundary = _case(
        44.0,
        as_of_minute=44,
        current_charging_age_s=14 * 60.0,
    )
    at_boundary = _case(
        44.0,
        as_of_minute=45,
        current_charging_age_s=15 * 60.0,
    )

    assert before_boundary.dhw_kwh[0] > 0.0
    assert before_boundary.dhw_kwh[1] > 0.0
    assert before_boundary.dhw_kwh[2] == 0.0
    assert at_boundary.dhw_kwh[0] > 0.0
    assert at_boundary.dhw_kwh[1:] == (0.0, 0.0)
    assert sum(at_boundary.dhw_kwh) < sum(before_boundary.dhw_kwh)


def test_legacy_lower_threshold_history_does_not_extend_active_cycle_past_target(
    tmp_path,
):
    legacy_cycles = tuple(
        item
        for day in range(23, 27)
        for item in _completed_cycle(day, target_temperature_c=44.0)
    )
    observed = (
        _history_slot(
            dt.datetime(2026, 9, 6, 3, 15, tzinfo=TZ),
            dhw_kwh=0.72,
            dhw_temperature_c=49.7,
            charging_fraction=1.0,
        ),
    )

    store = CompressedFeatureStore(tmp_path / "features.json.gz")
    store.initialize()
    store.upsert(legacy_cycles + observed)
    reloaded_store = CompressedFeatureStore(tmp_path / "features.json.gz")
    reloaded_store.initialize()

    forecast = _case(
        53.1,
        history_prefix=reloaded_store.load(0, 2**63 - 1),
        include_default_cycles=False,
        dhw_baseline=(0.30, 0.25, 0.20),
        current_power_w=2100.0,
    )

    assert forecast.dhw_kwh[1:] == (0.0, 0.0)


def test_cycle_efficiency_uses_historical_start_to_peak_lift():
    lower_target_cycles = tuple(
        item
        for day in range(23, 27)
        for item in _completed_cycle(
            day,
            target_temperature_c=50.0,
            peak_temperature_c=51.0,
        )
    )
    observed = (
        _history_slot(
            dt.datetime(2026, 9, 6, 3, 15, tzinfo=TZ),
            dhw_kwh=0.60,
            dhw_temperature_c=44.0,
            charging_fraction=1.0,
        ),
    )

    forecast = _case(
        49.7,
        history_prefix=lower_target_cycles + observed,
        include_default_cycles=False,
        dhw_baseline=(0.30, 0.25, 0.20),
        current_power_w=2100.0,
    )

    expected_specific_energy = 1.65 / (51.0 - 43.0)
    expected_remaining = expected_specific_energy * (53.0 - 49.7)
    assert sum(forecast.dhw_kwh) == pytest.approx(expected_remaining)
    assert forecast.dhw_kwh[1] > 0.1


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

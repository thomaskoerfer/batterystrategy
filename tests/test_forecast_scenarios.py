"""Behavioral tests for coherent forecast scenario generation."""

from __future__ import annotations

import datetime as dt

from custom_components.battery_strategy.contracts import (
    DataQuality,
    ForecastBundle,
    ForecastSlot,
    HistoricalFeatureSlot,
    LoadForecast,
    PvForecast,
    QuantileEnergy,
    SlotKey,
)
from custom_components.battery_strategy.forecasting.scenarios import (
    build_empirical_scenarios,
)

SLOT_MS = 15 * 60 * 1000


class MatureCalibration:
    def residuals_for(self, *_args, **_kwargs):
        return (-0.2,) * 6 + (0.2,) * 6


CALIBRATION = MatureCalibration()


def key(value: dt.datetime) -> SlotKey:
    start = int(value.timestamp() * 1000)
    return SlotKey(start, start + SLOT_MS)


def actual(value: dt.datetime, *, load: float, pv: float, ev: float):
    return HistoricalFeatureSlot(
        key(value), load, pv, 0.0, 0.0, 0.0, 0.0, ev, None, DataQuality()
    )


def test_empirical_scenarios_preserve_joint_weekly_paths_and_center_on_p50():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    slots = (key(start), key(start + dt.timedelta(minutes=15)))
    bundle = ForecastBundle(
        LoadForecast(
            "load",
            int(start.timestamp() * 1000),
            int(start.timestamp() * 1000),
            "load-v1",
            tuple(ForecastSlot(slot, QuantileEnergy(0.4)) for slot in slots),
        ),
        PvForecast(
            "pv",
            int(start.timestamp() * 1000),
            int(start.timestamp() * 1000),
            "pv-v1",
            tuple(ForecastSlot(slot, QuantileEnergy(0.2)) for slot in slots),
        ),
    )
    history = []
    for weeks, loads, pvs, evs in (
        (1, (0.2, 0.3), (0.1, 0.0), (0.0, 0.0)),
        (2, (0.6, 0.7), (0.3, 0.4), (0.0, 0.4)),
    ):
        for index in range(2):
            history.append(
                actual(
                    start
                    - dt.timedelta(weeks=weeks)
                    + dt.timedelta(minutes=15 * index),
                    load=loads[index],
                    pv=pvs[index],
                    ev=evs[index],
                )
            )

    result = build_empirical_scenarios(
        bundle,
        tuple(history),
        timezone="UTC",
        current_ev_charge_w=0.0,
        calibration=CALIBRATION,
        minimum_paths=2,
        maximum_paths=12,
    )

    assert result is not None
    assert len(result.scenarios) == 2
    assert sum(item.probability for item in result.scenarios) == 1.0
    assert result.scenarios[0].slots[0].load_no_ev_kwh == 0.2
    assert result.scenarios[1].slots[0].load_no_ev_kwh == 0.6
    assert result.scenarios[1].slots[0].ev_charge_kwh == 0.0
    assert result.scenarios[1].slots[1].ev_charge_kwh == 0.4


def test_empirical_scenarios_anchor_current_ev_power_exactly():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    slot = key(start)
    bundle = ForecastBundle(
        LoadForecast(
            "load",
            int(start.timestamp() * 1000),
            int(start.timestamp() * 1000),
            "load-v1",
            (ForecastSlot(slot, QuantileEnergy(0.4)),),
        ),
        PvForecast(
            "pv",
            int(start.timestamp() * 1000),
            int(start.timestamp() * 1000),
            "pv-v1",
            (ForecastSlot(slot, QuantileEnergy(0.2)),),
        ),
    )
    history = tuple(
        actual(
            start - dt.timedelta(weeks=weeks),
            load=0.4,
            pv=0.2,
            ev=0.75,
        )
        for weeks in (1, 2)
    )

    result = build_empirical_scenarios(
        bundle,
        history,
        timezone="UTC",
        current_ev_charge_w=1000.0,
        calibration=CALIBRATION,
        minimum_paths=2,
    )

    assert result is not None
    assert {path.slots[0].ev_charge_kwh for path in result.scenarios} == {0.25}


def test_empirical_scenarios_anchor_active_ev_in_first_slot_only():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    slots = (key(start),)
    generated = int(start.timestamp() * 1000)
    bundle = ForecastBundle(
        LoadForecast(
            "load",
            generated,
            generated,
            "load-v1",
            (ForecastSlot(slots[0], QuantileEnergy(0.4)),),
        ),
        PvForecast(
            "pv",
            generated,
            generated,
            "pv-v1",
            (ForecastSlot(slots[0], QuantileEnergy(0.0)),),
        ),
    )
    history = tuple(
        actual(start - dt.timedelta(weeks=weeks), load=0.4, pv=0.0, ev=0.2)
        for weeks in (1, 2)
    )

    result = build_empirical_scenarios(
        bundle,
        history,
        timezone="UTC",
        current_ev_charge_w=4000.0,
        calibration=CALIBRATION,
        minimum_paths=2,
    )

    assert result is not None
    assert all(item.slots[0].ev_charge_kwh == 1.0 for item in result.scenarios)


def test_empirical_scenarios_never_read_history_after_generation_time():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    slot = key(start)
    generated = int(start.timestamp() * 1000)
    bundle = ForecastBundle(
        LoadForecast(
            "load",
            generated,
            generated,
            "load-v1",
            (ForecastSlot(slot, QuantileEnergy(0.4)),),
        ),
        PvForecast(
            "pv",
            generated,
            generated,
            "pv-v1",
            (ForecastSlot(slot, QuantileEnergy(0.0)),),
        ),
    )
    valid = tuple(
        actual(start - dt.timedelta(weeks=weeks), load=0.4, pv=0.0, ev=0.0)
        for weeks in (1, 2)
    )
    future = actual(start + dt.timedelta(minutes=15), load=9.0, pv=9.0, ev=9.0)

    result = build_empirical_scenarios(
        bundle,
        (*valid, future),
        timezone="UTC",
        calibration=CALIBRATION,
        minimum_paths=2,
    )

    assert result is not None
    assert len(result.scenarios) == 2


def test_empirical_scenarios_require_mature_residual_calibration():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    slot = key(start)
    generated = int(start.timestamp() * 1000)
    bundle = ForecastBundle(
        LoadForecast(
            "load",
            generated,
            generated,
            "load-v1",
            (ForecastSlot(slot, QuantileEnergy(0.4)),),
        ),
        PvForecast(
            "pv",
            generated,
            generated,
            "pv-v1",
            (ForecastSlot(slot, QuantileEnergy(0.0)),),
        ),
    )
    history = tuple(
        actual(start - dt.timedelta(weeks=weeks), load=0.4, pv=0.0, ev=0.0)
        for weeks in range(1, 6)
    )

    assert (
        build_empirical_scenarios(bundle, history, timezone="UTC", calibration=None)
        is None
    )


def test_empirical_scenarios_search_past_incompatible_ev_weeks():
    start = dt.datetime(2026, 9, 21, 18, 0, tzinfo=dt.UTC)
    slot = key(start)
    generated = int(start.timestamp() * 1000)
    bundle = ForecastBundle(
        LoadForecast(
            "load",
            generated,
            generated,
            "load-v1",
            (ForecastSlot(slot, QuantileEnergy(0.4)),),
        ),
        PvForecast(
            "pv",
            generated,
            generated,
            "pv-v1",
            (ForecastSlot(slot, QuantileEnergy(0.0)),),
        ),
    )
    history = tuple(
        actual(
            start - dt.timedelta(weeks=weeks),
            load=0.4,
            pv=0.0,
            ev=0.2 if weeks <= 12 else 0.0,
        )
        for weeks in range(1, 17)
    )

    result = build_empirical_scenarios(
        bundle,
        history,
        timezone="UTC",
        current_ev_charge_w=0.0,
        calibration=CALIBRATION,
        minimum_paths=4,
        maximum_paths=12,
    )

    assert result is not None
    assert len(result.scenarios) == 4
    assert all(path.scenario_id.startswith("week-1") for path in result.scenarios)

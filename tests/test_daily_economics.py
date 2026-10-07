"""Tests for actual-plus-plan full-day economics."""

import datetime as dt
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.contracts import (
    HistoricalFeatureSlot,
    SlotKey,
)
from custom_components.battery_strategy.daily_economics import (
    build_daily_cost_projection,
)
from custom_components.battery_strategy.plan_models import PlanPoint, StrategyPlan

ZONE = ZoneInfo("Europe/Berlin")


def _point(start: dt.datetime, *, price: float = 40.0) -> PlanPoint:
    return PlanPoint(
        ts_ms=int(start.timestamp() * 1000),
        date=start.date().isoformat(),
        price_ct=price,
        load_fc_w=1000,
        pv_fc_w=0,
        grid_import_fc_w=500,
        grid_export_fc_w=0,
        grid_net_fc_w=500,
        mode="output",
        power_w=500,
        charge_fc_w=0,
        discharge_fc_w=500,
        soc_pct=50.0,
    )


def _day_points(day: dt.date, *, limit: int | None = None) -> tuple[PlanPoint, ...]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=ZONE)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=ZONE)
    starts = range(
        int(start.timestamp()),
        int(end.timestamp()),
        int(dt.timedelta(minutes=15).total_seconds()),
    )
    points = tuple(
        _point(dt.datetime.fromtimestamp(timestamp, dt.UTC).astimezone(ZONE))
        for timestamp in starts
    )
    return points if limit is None else points[:limit]


def test_today_combines_finalized_ev_free_actual_with_remaining_plan():
    day = dt.date(2026, 10, 7)
    midnight = dt.datetime.combine(day, dt.time.min, tzinfo=ZONE)
    slot = SlotKey(
        int(midnight.timestamp() * 1000),
        int((midnight + dt.timedelta(minutes=15)).timestamp() * 1000),
    )
    history = (
        HistoricalFeatureSlot(
            slot=slot,
            house_load_no_ev_kwh=0.25,
            pv_generation_kwh=0.0,
            grid_import_kwh=0.55,
            grid_export_kwh=0.0,
            battery_charge_kwh=0.0,
            battery_discharge_kwh=0.0,
            ev_charge_kwh=0.50,
            price_ct_per_kwh=40.0,
        ),
    )
    plan = StrategyPlan(
        _day_points(day) + _day_points(day + dt.timedelta(days=1)),
        "output",
        500,
        "test",
    )

    result = build_daily_cost_projection(
        history,
        plan,
        local_date=day,
        as_of_ms=slot.end_ms,
        timezone="Europe/Berlin",
        export_value_ct_per_kwh=0.0,
    )

    # Actual baseline 0.25 kWh plus 95 planned quarter-hours.
    assert result[day.isoformat()].base_eur == 9.6
    # Actual grid excluding 0.50 kWh EV is 0.05 kWh, then planned grid.
    assert result[day.isoformat()].with_bat_eur == 4.77
    assert result[(day + dt.timedelta(days=1)).isoformat()].base_eur == 9.6
    assert result[(day + dt.timedelta(days=1)).isoformat()].with_bat_eur == 4.8


def test_missing_actual_price_makes_today_unavailable():
    day = dt.date(2026, 10, 7)
    midnight = dt.datetime.combine(day, dt.time.min, tzinfo=ZONE)
    slot = SlotKey(
        int(midnight.timestamp() * 1000),
        int((midnight + dt.timedelta(minutes=15)).timestamp() * 1000),
    )
    actual = HistoricalFeatureSlot(
        slot=slot,
        house_load_no_ev_kwh=0.25,
        pv_generation_kwh=0.0,
        grid_import_kwh=0.25,
        grid_export_kwh=0.0,
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        ev_charge_kwh=0.0,
        price_ct_per_kwh=None,
    )
    result = build_daily_cost_projection(
        (actual,),
        StrategyPlan(
            _day_points(day) + _day_points(day + dt.timedelta(days=1)),
            "idle",
            0,
            "test",
        ),
        local_date=day,
        as_of_ms=slot.end_ms,
        timezone="Europe/Berlin",
        export_value_ct_per_kwh=0.0,
    )

    assert day.isoformat() not in result


def test_incomplete_dst_tomorrow_is_unavailable_instead_of_partial():
    day = dt.date(2026, 10, 24)
    tomorrow = day + dt.timedelta(days=1)  # 25-hour local day
    plan = StrategyPlan(
        _day_points(day) + _day_points(tomorrow, limit=96),
        "idle",
        0,
        "test",
    )

    result = build_daily_cost_projection(
        (),
        plan,
        local_date=day,
        as_of_ms=int(
            dt.datetime.combine(day, dt.time.min, tzinfo=ZONE).timestamp() * 1000
        ),
        timezone="Europe/Berlin",
        export_value_ct_per_kwh=0.0,
    )

    assert tomorrow.isoformat() not in result

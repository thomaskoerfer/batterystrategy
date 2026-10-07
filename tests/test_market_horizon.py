"""Regression tests for the rolling firm/proxy market boundary."""

import datetime as dt
from itertools import pairwise
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.market_context import (
    MarketContextConfig,
    MarketContextService,
)
from custom_components.battery_strategy.planning_state import MarketState
from custom_components.battery_strategy.runtime_market_data import TariffInterval


def service():
    return MarketContextService(MarketContextConfig(dt.UTC, 0.8, 1.0))


def test_rolling_horizon_preserves_partial_firm_prices(monkeypatch):
    start = dt.datetime(2026, 9, 22, 23, 45, tzinfo=dt.UTC)
    firm = [
        TariffInterval(start + dt.timedelta(minutes=15 * index), 0.20 + index / 100)
        for index in range(2)
    ]

    def proxy(_intervals, _days, _prior, target):
        midnight = dt.datetime.combine(target, dt.time.min, tzinfo=dt.UTC)
        return [
            TariffInterval(
                midnight + dt.timedelta(minutes=15 * index), 0.50, "eex_proxy"
            )
            for index in range(96)
        ]

    candidate = service()
    monkeypatch.setattr(candidate, "build_eex_proxy_day_prices", proxy)
    result, source = candidate.build_rolling_horizon_prices(firm, {}, start, 8)

    assert len(result) == 8
    assert source == "mixed"
    assert result[:2] == firm
    assert all(item.source == "eex_proxy" for item in result[2:])


def test_rolling_horizon_has_constant_length_across_midnight(monkeypatch):
    candidate = service()

    def proxy(_intervals, _days, _prior, target):
        midnight = dt.datetime.combine(target, dt.time.min, tzinfo=dt.UTC)
        return [
            TariffInterval(
                midnight + dt.timedelta(minutes=15 * index), 0.30, "eex_proxy"
            )
            for index in range(96)
        ]

    monkeypatch.setattr(candidate, "build_eex_proxy_day_prices", proxy)
    before, _ = candidate.build_rolling_horizon_prices(
        (), {}, dt.datetime(2026, 9, 22, 23, 45, tzinfo=dt.UTC), 192
    )
    after, _ = candidate.build_rolling_horizon_prices(
        (), {}, dt.datetime(2026, 9, 23, 0, 0, tzinfo=dt.UTC), 192
    )

    assert len(before) == len(after) == 192
    assert before[0].starts_at + dt.timedelta(minutes=15) == after[0].starts_at


def test_rolling_horizon_remains_a_unique_utc_grid_across_dst(monkeypatch):
    candidate = MarketContextService(
        MarketContextConfig(ZoneInfo("Europe/Berlin"), 0.8, 1.0)
    )

    def proxy(_intervals, _days, _prior, target):
        midnight = dt.datetime.combine(
            target, dt.time.min, tzinfo=ZoneInfo("Europe/Berlin")
        )
        return [
            TariffInterval(
                midnight + dt.timedelta(minutes=15 * index), 0.30, "eex_proxy"
            )
            for index in range(96)
        ]

    monkeypatch.setattr(candidate, "build_eex_proxy_day_prices", proxy)
    for start in (
        dt.datetime(2026, 3, 28, 12, tzinfo=ZoneInfo("Europe/Berlin")),
        dt.datetime(2026, 10, 24, 12, tzinfo=ZoneInfo("Europe/Berlin")),
    ):
        result, _ = candidate.build_rolling_horizon_prices((), {}, start, 192)
        timestamps = [item.starts_at.astimezone(dt.UTC) for item in result]
        assert len(result) == len(set(timestamps)) == 192
        assert all(
            later - earlier == dt.timedelta(minutes=15)
            for earlier, later in pairwise(timestamps)
        )


def test_proxy_day_is_stable_for_same_eex_vintage_across_midnight():
    candidate = service()
    state = MarketState()
    target = dt.date(2026, 10, 8)
    context = {
        "base": {"settl_ct_kwh": 8.0, "trade_date": "2026-10-06"},
        "peak": {"settl_ct_kwh": 10.0, "trade_date": "2026-10-06"},
    }
    eex = {
        dt.date(2026, 10, 7).isoformat(): context,
        target.isoformat(): context,
    }
    reference = [
        TariffInterval(
            dt.datetime(2026, 10, 6, tzinfo=dt.UTC) + dt.timedelta(minutes=15 * index),
            0.20 + (index % 12) / 1000,
        )
        for index in range(96)
    ]

    before, _ = candidate.build_rolling_horizon_prices(
        reference,
        eex,
        dt.datetime(2026, 10, 7, 23, 45, tzinfo=dt.UTC),
        100,
        state=state,
    )
    # The provider window changes at midnight. The cached delivery-day curve
    # must not change while its EEX information vintage remains identical.
    after, _ = candidate.build_rolling_horizon_prices(
        (),
        eex,
        dt.datetime(2026, 10, 8, 0, 0, tzinfo=dt.UTC),
        96,
        state=state,
    )

    shared_before = {
        int(item.timestamp): item.price_eur_per_kwh
        for item in before
        if item.starts_at.date() == target
    }
    shared_after = {
        int(item.timestamp): item.price_eur_per_kwh
        for item in after
        if item.starts_at.date() == target
    }
    assert shared_before
    assert shared_before.items() <= shared_after.items()


def test_proxy_day_rebuilds_when_eex_vintage_changes():
    candidate = service()
    state = MarketState()
    target = dt.date(2026, 10, 8)
    first = {
        target.isoformat(): {
            "base": {"settl_ct_kwh": 8.0, "trade_date": "2026-10-06"},
            "peak": {"settl_ct_kwh": 10.0, "trade_date": "2026-10-06"},
        }
    }
    second = {
        target.isoformat(): {
            "base": {"settl_ct_kwh": 12.0, "trade_date": "2026-10-07"},
            "peak": {"settl_ct_kwh": 15.0, "trade_date": "2026-10-07"},
        }
    }
    start = dt.datetime(2026, 10, 8, tzinfo=dt.UTC)

    old, _ = candidate.build_rolling_horizon_prices((), first, start, 96, state=state)
    new, _ = candidate.build_rolling_horizon_prices((), second, start, 96, state=state)

    assert [item.price_eur_per_kwh for item in old] != [
        item.price_eur_per_kwh for item in new
    ]


def test_eex_refresh_failure_retains_last_known_vintage(monkeypatch):
    candidate = service()
    local_now = dt.datetime(2026, 10, 7, 12, tzinfo=dt.UTC)
    today = local_now.date().isoformat()
    retained = {
        "base": {"settl_ct_kwh": 8.0, "trade_date": "2026-10-06"},
        "peak": {"settl_ct_kwh": 10.0, "trade_date": "2026-10-06"},
    }
    state = MarketState({"fetched_at_ts": 0.0, "days": {today: retained}})
    monkeypatch.setattr(
        candidate,
        "_eex_filter_rows",
        lambda _product: (_ for _ in ()).throw(OSError("temporary outage")),
    )

    refreshed = candidate.get_eex_day_context(state, local_now)

    assert refreshed[today] == retained


def test_eex_ttl_cache_refreshes_when_rolling_window_needs_new_day(monkeypatch):
    candidate = service()
    local_now = dt.datetime(2026, 10, 8, 0, 15, tzinfo=dt.UTC)
    cached_dates = {
        (
            local_now.date() - dt.timedelta(days=1) + dt.timedelta(days=offset)
        ).isoformat(): {}
        for offset in range(4)
    }
    state = MarketState({"fetched_at_ts": local_now.timestamp(), "days": cached_dates})
    calls = []
    monkeypatch.setattr(
        candidate,
        "_eex_filter_rows",
        lambda product: calls.append(product) or [],
    )

    candidate.get_eex_day_context(state, local_now)

    assert calls == ["Base", "Peak"]


def test_partial_eex_refresh_keeps_complete_previous_vintage(monkeypatch):
    candidate = service()
    local_now = dt.datetime(2026, 10, 7, 12, tzinfo=dt.UTC)
    today = local_now.date().isoformat()
    retained = {
        "base": {"settl_ct_kwh": 8.0, "trade_date": "2026-10-06"},
        "peak": {"settl_ct_kwh": 10.0, "trade_date": "2026-10-06"},
    }
    state = MarketState({"fetched_at_ts": 0.0, "days": {today: retained}})
    monkeypatch.setattr(
        candidate,
        "_eex_filter_rows",
        lambda product: [
            {
                "delivery_date": today,
                "shortCode": "test",
                "maturity": "202610",
                "product": product,
            }
        ],
    )
    monkeypatch.setattr(
        candidate,
        "_eex_fetch_settlement",
        lambda row, _trade_date: (
            {"settl_ct_kwh": 9.0, "trade_date": "2026-10-07"}
            if row["product"] == "Base"
            else None
        ),
    )

    refreshed = candidate.get_eex_day_context(state, local_now)

    assert refreshed[today] == retained


def test_commercial_policy_uses_rolling_continuation_not_calendar_tomorrow():
    candidate = service()
    start = dt.datetime(2026, 10, 7, 23, 45, tzinfo=dt.UTC)
    market = [
        TariffInterval(start + dt.timedelta(minutes=15 * index), 0.30)
        for index in range(288)
    ]
    # One cheap anchor and an expensive continuation make the policy visible.
    market[20] = TariffInterval(market[20].starts_at, 0.15)
    for index in range(192, 288):
        market[index] = TariffInterval(market[index].starts_at, 0.45)
    samples = [
        {
            "ts": dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC).timestamp()
            - 7 * 86_400 * index,
            "price_ct": 20.0 + index,
        }
        for index in range(20)
    ]

    before = candidate.build_plan_metadata(
        market[:192], samples, continuation_intervals=market[192:]
    )["price_stats"]
    after = candidate.build_plan_metadata(
        market[1:193], samples, continuation_intervals=market[193:]
    )["price_stats"]

    assert before["discharge_floor_ct"] is not None
    assert before["terminal_value_ct"] > 0.0
    assert before["discharge_floor_ct"] == after["discharge_floor_ct"]
    assert before["cheap_anchor_ct"] == after["cheap_anchor_ct"]
    assert before["terminal_value_ct"] == after["terminal_value_ct"]

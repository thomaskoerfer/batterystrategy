"""Regression tests for the rolling firm/proxy market boundary."""

import datetime as dt
from itertools import pairwise
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.market_context import (
    MarketContextConfig,
    MarketContextService,
)
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

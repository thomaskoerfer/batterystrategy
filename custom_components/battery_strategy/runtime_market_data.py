"""Normalize the tariff snapshot captured by the Home Assistant adapter."""

from __future__ import annotations

import bisect
import datetime as dt


def read_tibber_intervals_all(runtime):
    if runtime.price_intervals:
        merged = {}
        for item in runtime.price_intervals:
            try:
                value = float(
                    item.get("price_per_kwh", item.get("price", item.get("total")))
                )
                timestamp = (
                    item.get("start_time") or item.get("startsAt") or item.get("start")
                )
                parsed = dt.datetime.fromisoformat(
                    str(timestamp).replace("Z", "+00:00")
                )
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=runtime.settings.timezone)
                price_eur = value / 100.0 if value >= 2.0 else value
                merged[parsed.timestamp()] = {
                    "ts": parsed.timestamp(),
                    "dt": parsed,
                    "price_eur": price_eur,
                }
            except (AttributeError, TypeError, ValueError):
                continue
        return [merged[key] for key in sorted(merged)]
    return []


def read_tibber_intervals_for_dates(runtime, date_set):
    intervals = read_tibber_intervals_all(runtime)
    return [it for it in intervals if it["dt"].date().isoformat() in date_set]


def read_tibber_future_price_stats(runtime, now_local):
    date_set = {
        now_local.date().isoformat(),
        (now_local.date() + dt.timedelta(days=1)).isoformat(),
    }
    intervals = read_tibber_intervals_for_dates(runtime, date_set)
    if not intervals:
        return None
    future = [it["price_eur"] * 100.0 for it in intervals if it["dt"] >= now_local]
    if not future:
        return None
    return {"min_ct": min(future), "max_ct": max(future)}


def local_date_set_between(timezone, start_ts, end_ts):
    start_day = (
        dt.datetime.fromtimestamp(float(start_ts), dt.timezone.utc)
        .astimezone(timezone)
        .date()
    )
    end_day = (
        dt.datetime.fromtimestamp(float(end_ts), dt.timezone.utc)
        .astimezone(timezone)
        .date()
    )
    days = set()
    cur = start_day
    while cur <= end_day:
        days.add(cur.isoformat())
        cur += dt.timedelta(days=1)
    return days


def build_tibber_price_index(runtime, date_set):
    intervals = read_tibber_intervals_for_dates(runtime, date_set)
    pairs = sorted(
        (float(it["dt"].timestamp()), float(it["price_eur"])) for it in intervals
    )
    if not pairs:
        return [], []
    return [p[0] for p in pairs], [p[1] for p in pairs]


def tibber_price_eur_at(ts, price_ts, price_vals):
    if not price_ts:
        return None
    i = bisect.bisect_right(price_ts, float(ts)) - 1
    if i < 0:
        return None
    return float(price_vals[i])

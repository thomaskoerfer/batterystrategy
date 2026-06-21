"""Pricing helpers for Battery Strategy planning."""

from __future__ import annotations

import datetime as dt
import glob
import json
from pathlib import Path

from .plan_models import PricePoint


def price_points_from_profile(profile: list | None) -> list[PricePoint]:
    """Convert a HA chart profile into price points."""
    points: list[PricePoint] = []
    for item in profile or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            points.append(PricePoint(int(float(item[0])), float(item[1])))
        except (TypeError, ValueError):
            continue
    return sorted(points, key=lambda p: p.ts_ms)


def read_tibber_price_points(storage_glob: str, now: dt.datetime, horizon_h: int = 48) -> list[PricePoint]:
    """Read future prices from Tibber Prices storage files when available."""
    end = now + dt.timedelta(hours=horizon_h)
    points: list[PricePoint] = []
    for filename in sorted(glob.glob(storage_glob)):
        path = Path(filename)
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        points.extend(_extract_tibber_points(obj, now, end))
    dedup: dict[int, PricePoint] = {p.ts_ms: p for p in points}
    return sorted(dedup.values(), key=lambda p: p.ts_ms)


def _extract_tibber_points(obj: dict, start: dt.datetime, end: dt.datetime) -> list[PricePoint]:
    """Extract price intervals from known Tibber Prices storage shapes."""
    records: list[dict] = []
    for group in obj.get("data", {}).get("fetch_groups", []):
        records.extend(group.get("intervals", []) or [])
        records.extend(group.get("prices", []) or [])
    records.extend(obj.get("data", {}).get("intervals", []) or [])
    records.extend(obj.get("intervals", []) or [])

    points: list[PricePoint] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        raw_start = rec.get("start") or rec.get("startsAt") or rec.get("starts_at") or rec.get("from") or rec.get("time")
        raw_price = rec.get("total") or rec.get("price") or rec.get("energy") or rec.get("value")
        if raw_start is None or raw_price is None:
            continue
        try:
            ts = dt.datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=start.tzinfo)
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price < 2.0:
            price *= 100.0
        if start <= ts <= end:
            points.append(PricePoint(int(ts.timestamp() * 1000), price))
    return points


def price_at(points: list[PricePoint], ts_ms: int, fallback_ct: float = 30.0) -> float:
    """Return the latest price at or before a timestamp."""
    if not points:
        return fallback_ct
    best = points[0]
    for point in points:
        if point.ts_ms > ts_ms:
            break
        best = point
    return float(best.price_ct)


def price_stats(points: list[PricePoint]) -> dict[str, float | None]:
    """Return simple price statistics in ct/kWh."""
    vals = [float(p.price_ct) for p in points if p.price_ct is not None]
    if not vals:
        return {"min": None, "max": None, "avg": None, "p_low": None, "p_high": None}
    ordered = sorted(vals)
    return {
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "avg": round(sum(vals) / len(vals), 3),
        "p_low": round(_quantile(ordered, 0.2), 3),
        "p_high": round(_quantile(ordered, 0.8), 3),
    }


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q))))
    return sorted_vals[idx]

"""Pure presentation of optimizer plans for Home Assistant entities."""

from __future__ import annotations


def build_price_profile(intervals, date_str):
    arr = [it for it in intervals if it.starts_at.date().isoformat() == date_str]
    return [
        [int(it.starts_at.timestamp() * 1000), round(it.price_eur_per_kwh * 100.0, 3)]
        for it in arr
    ]


def merge_actual_and_future_profile(actual_points, future_points, date_str, now_ts_ms):
    past = [
        p
        for p in actual_points
        if p.get("date") == date_str and p.get("ts_ms", 0) <= now_ts_ms
    ]
    future = [
        p
        for p in future_points
        if p.get("date") == date_str and p.get("ts_ms", 0) > now_ts_ms
    ]
    merged = sorted(past + future, key=lambda x: x.get("ts_ms", 0))
    return {
        "soc": [[p["ts_ms"], p["soc_pct"]] for p in merged if "soc_pct" in p],
        "power": [[p["ts_ms"], p["power_w"]] for p in merged if "power_w" in p],
        "charge_power": [
            [p["ts_ms"], p["charge_fc_w"]] for p in merged if "charge_fc_w" in p
        ],
        "pv_charge_power": [[p["ts_ms"], p.get("pv_charge_fc_w", 0.0)] for p in merged],
        "grid_charge_power": [
            [p["ts_ms"], p.get("grid_charge_fc_w", 0.0)] for p in merged
        ],
        "required_charge_power": [
            [p["ts_ms"], p.get("required_charge_fc_w", 0.0)] for p in merged
        ],
        "discharge_power": [
            [p["ts_ms"], p["discharge_fc_w"]] for p in merged if "discharge_fc_w" in p
        ],
        "discharge_budget_kwh": [
            [p["ts_ms"], p.get("discharge_budget_kwh", 0.0)] for p in merged
        ],
    }


def compress_points_hourly(points):
    buckets = {}
    for p in points:
        hour_ms = (int(p.get("ts_ms", 0)) // 3600000) * 3600000
        b = buckets.setdefault(
            hour_ms,
            {
                "n": 0,
                "price_ct": 0.0,
                "soc_pct": 0.0,
                "power_w": 0.0,
                "charge_fc_w": 0.0,
                "discharge_fc_w": 0.0,
                "pv_fc_w": 0.0,
                "grid_import_fc_w": 0.0,
                "grid_export_fc_w": 0.0,
                "grid_net_fc_w": 0.0,
                "date": p.get("date"),
            },
        )
        b["n"] += 1
        b["price_ct"] += float(p.get("price_ct", 0.0))
        b["soc_pct"] += float(p.get("soc_pct", 0.0))
        b["power_w"] += float(p.get("power_w", 0.0))
        b["charge_fc_w"] += float(p.get("charge_fc_w", 0.0))
        b["discharge_fc_w"] += float(p.get("discharge_fc_w", 0.0))
        b["discharge_budget_kwh"] = max(
            float(b.get("discharge_budget_kwh", 0.0)),
            float(p.get("discharge_budget_kwh", 0.0)),
        )
        b["pv_fc_w"] += float(p.get("pv_fc_w", 0.0))
        b["grid_import_fc_w"] += float(p.get("grid_import_fc_w", 0.0))
        b["grid_export_fc_w"] += float(p.get("grid_export_fc_w", 0.0))
        b["grid_net_fc_w"] += float(p.get("grid_net_fc_w", 0.0))

    out = []
    for ts in sorted(buckets.keys()):
        b = buckets[ts]
        n = max(1, b["n"])
        out.append(
            {
                "ts_ms": ts,
                "date": b.get("date"),
                "price_ct": round(b["price_ct"] / n, 3),
                "soc_pct": round(b["soc_pct"] / n, 2),
                "power_w": round(b["power_w"] / n, 1),
                "charge_fc_w": round(b["charge_fc_w"] / n, 1),
                "discharge_fc_w": round(b["discharge_fc_w"] / n, 1),
                "discharge_budget_kwh": round(
                    float(b.get("discharge_budget_kwh", 0.0)), 3
                ),
                "pv_fc_w": round(b["pv_fc_w"] / n, 1),
                "grid_import_fc_w": round(b["grid_import_fc_w"] / n, 1),
                "grid_export_fc_w": round(b["grid_export_fc_w"] / n, 1),
                "grid_net_fc_w": round(b["grid_net_fc_w"] / n, 1),
            }
        )
    return out


def build_anchored_hourly_series(hourly_points, key, now_ts_ms, anchor_w=None):
    current_hour_ms = (int(now_ts_ms) // 3600000) * 3600000
    out = []
    if anchor_w is not None:
        out.append([int(now_ts_ms), round(float(anchor_w), 1)])
    for p in hourly_points:
        ts_ms = int(p.get("ts_ms", 0))
        if ts_ms <= current_hour_ms:
            continue
        out.append([ts_ms, round(float(p.get(key, 0.0)), 1)])
    return out


def split_profile(points, date_str):
    arr = [p for p in points if p.get("date") == date_str]
    return {
        "price": [[p["ts_ms"], p["price_ct"]] for p in arr],
        "soc": [[p["ts_ms"], p["soc_pct"]] for p in arr],
        "power": [[p["ts_ms"], p["power_w"]] for p in arr],
        "charge_power": [[p["ts_ms"], p["charge_fc_w"]] for p in arr],
        "pv_charge_power": [[p["ts_ms"], p.get("pv_charge_fc_w", 0.0)] for p in arr],
        "grid_charge_power": [
            [p["ts_ms"], p.get("grid_charge_fc_w", 0.0)] for p in arr
        ],
        "required_charge_power": [
            [p["ts_ms"], p.get("required_charge_fc_w", 0.0)] for p in arr
        ],
        "discharge_power": [[p["ts_ms"], p["discharge_fc_w"]] for p in arr],
        "discharge_budget_kwh": [
            [p["ts_ms"], p.get("discharge_budget_kwh", 0.0)] for p in arr
        ],
        "load_fc_power": [[p["ts_ms"], p["load_fc_w"]] for p in arr],
        "pv_fc_power": [[p["ts_ms"], p["pv_fc_w"]] for p in arr],
        "grid_import_fc_power": [[p["ts_ms"], p["grid_import_fc_w"]] for p in arr],
        "grid_export_fc_power": [[p["ts_ms"], p["grid_export_fc_w"]] for p in arr],
        "grid_net_fc_power": [[p["ts_ms"], p["grid_net_fc_w"]] for p in arr],
    }


def build_published_plan_profiles(
    actual_points,
    future_points,
    today,
    tomorrow,
    now_ts_ms,
):
    """Publish canonical plan data without projecting live commands forward."""
    forecast_today = split_profile(future_points, today)
    forecast_tomorrow = split_profile(future_points, tomorrow)
    profile_today = merge_actual_and_future_profile(
        actual_points, future_points, today, now_ts_ms
    )
    profile_tomorrow = merge_actual_and_future_profile(
        actual_points, future_points, tomorrow, now_ts_ms
    )
    return forecast_today, forecast_tomorrow, profile_today, profile_tomorrow


def derive_planned_dispatch(first_plan):
    if not first_plan:
        return "idle", 0

    plan_mode = first_plan.get("mode", "idle")
    plan_power = round(abs(float(first_plan.get("power_w", 0.0))))

    if plan_mode == "charge":
        charge_mode = (
            "charge_grid"
            if float(first_plan.get("grid_charge_fc_w", 0.0)) > 0.0
            else "charge_pv_surplus"
        )
        return charge_mode, plan_power
    if plan_mode == "discharge":
        return "discharge_planned", plan_power
    return "idle", 0

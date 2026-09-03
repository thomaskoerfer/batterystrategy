"""Normalized measurements and bounded history views for planning."""

from __future__ import annotations

import datetime as dt

E_PRICE_CURRENT = "price_current"
E_PRICE_EUR = "price_eur"
E_GRID_IMPORT = "grid_import"
E_GRID_EXPORT = "grid_export"
E_PV_POWER = "pv_power"
E_BATTERY_SOC = "battery_soc"
E_BATTERY_MIN_SOC = "battery_min_soc"
E_BATTERY_POWER = "battery_power"
E_BATTERY_INPUT_ENERGY = "battery_input_energy"
E_BATTERY_OUTPUT_ENERGY = "battery_output_energy"
E_PV_NEXT_HOUR_ENERGY = "pv_next_hour_energy"
E_PV_NEXT_HOUR_POWER = "pv_next_hour_power"
E_PV_TOMORROW_ENERGY = "pv_tomorrow_energy"
E_WEATHER_CLOUD = "weather_cloud"
E_WEATHER_RADIATION = "weather_radiation"
E_HEAT_PUMP_POWER = "heat_pump_power"
E_EV_POWER = "ev_power"
E_EV_STATUS = "ev_status"


def get_latest_states(runtime, entity_ids):
    """Return only the immutable live-state snapshot captured by the adapter."""
    return {entity_id: runtime.states.get(entity_id) for entity_id in entity_ids}


def fetch_sensor_series_many(runtime, entity_ids, cutoff_ts):
    """Return bounded Recorder history captured through the HA adapter."""
    cutoff = float(cutoff_ts)
    return {
        entity_id: [
            (float(timestamp), float(value))
            for timestamp, value in runtime.history_series.get(entity_id, ())
            if float(timestamp) >= cutoff
        ]
        for entity_id in entity_ids
    }


def fetch_sensor_series(runtime, entity_id, cutoff_ts):
    """Return one normalized series from the captured history snapshot."""
    return fetch_sensor_series_many(runtime, [entity_id], cutoff_ts)[entity_id]


def fetch_net_actual_profile(runtime, hours=48):
    cutoff_ts = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    series_map = fetch_sensor_series_many(
        runtime,
        [
            E_GRID_IMPORT,
            E_GRID_EXPORT,
        ],
        cutoff_ts,
    )
    imp = series_map[E_GRID_IMPORT]
    exp = series_map[E_GRID_EXPORT]
    buckets = {}
    for ts, v in imp:
        h = int(ts // 3600) * 3600
        buckets.setdefault(h, {"imp": [], "exp": []})["imp"].append(float(v))
    for ts, v in exp:
        h = int(ts // 3600) * 3600
        buckets.setdefault(h, {"imp": [], "exp": []})["exp"].append(float(v))
    out = []
    for h in sorted(buckets.keys()):
        rec = buckets[h]
        imp_avg = (sum(rec["imp"]) / len(rec["imp"])) if rec["imp"] else 0.0
        exp_avg = (sum(rec["exp"]) / len(rec["exp"])) if rec["exp"] else 0.0
        out.append([int(h * 1000), round(imp_avg - exp_avg, 1)])
    return out


def fetch_pv_actual_profile(runtime, hours=48):
    cutoff_ts = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    pv = fetch_sensor_series_many(runtime, [E_PV_POWER], cutoff_ts)[E_PV_POWER]
    buckets = {}
    for ts, v in pv:
        h = int(ts // 3600) * 3600
        buckets.setdefault(h, []).append(max(0.0, float(v)))
    out = []
    for h in sorted(buckets.keys()):
        vals = buckets[h]
        avg = (sum(vals) / len(vals)) if vals else 0.0
        out.append([int(h * 1000), round(avg, 1)])
    return out


def build_house_actual_profile_from_samples(samples, hours=48, now_ts=None):
    now_ts = float(
        now_ts if now_ts is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    )
    cutoff_ts = now_ts - hours * 3600
    buckets = {}
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        ts = float(sample.get("ts", 0.0) or 0.0)
        if ts < cutoff_ts:
            continue
        load_w = as_float(sample.get("load_w"), None)
        if load_w is None:
            load_w = as_float(sample.get("house_w"), None)
        if load_w is None:
            continue
        h = int(ts // 3600) * 3600
        buckets.setdefault(h, []).append(max(0.0, float(load_w)))
    out = []
    for h in sorted(buckets.keys()):
        vals = buckets[h]
        avg = (sum(vals) / len(vals)) if vals else 0.0
        out.append([int(h * 1000), round(avg, 1)])
    return out


def fetch_house_actual_profile(runtime, hours=48, samples=None):
    sample_profile = build_house_actual_profile_from_samples(samples, hours)
    if len(sample_profile) >= 3:
        return sample_profile

    cutoff_ts = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    series_map = fetch_sensor_series_many(
        runtime,
        [
            E_GRID_IMPORT,
            E_GRID_EXPORT,
            E_PV_POWER,
            E_EV_POWER,
            E_BATTERY_POWER,
        ],
        cutoff_ts,
    )
    imp = series_map[E_GRID_IMPORT]
    exp = series_map[E_GRID_EXPORT]
    pv = series_map[E_PV_POWER]
    wallbox = series_map[E_EV_POWER]
    bat = series_map[E_BATTERY_POWER]
    buckets = {}
    for ts, v in imp:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})[
            "imp"
        ].append(float(v))
    for ts, v in exp:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})[
            "exp"
        ].append(float(v))
    for ts, v in pv:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})[
            "pv"
        ].append(max(0.0, float(v)))
    for ts, v in wallbox:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})[
            "wb"
        ].append(max(0.0, float(v) * 1000.0))
    for ts, v in bat:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})[
            "bat"
        ].append(float(v))
    out = []
    for b in sorted(buckets.keys()):
        rec = buckets[b]
        imp_avg = (sum(rec["imp"]) / len(rec["imp"])) if rec["imp"] else 0.0
        exp_avg = (sum(rec["exp"]) / len(rec["exp"])) if rec["exp"] else 0.0
        pv_avg = (sum(rec["pv"]) / len(rec["pv"])) if rec["pv"] else 0.0
        wb_avg = (sum(rec["wb"]) / len(rec["wb"])) if rec["wb"] else 0.0
        bat_avg = (sum(rec["bat"]) / len(rec["bat"])) if rec["bat"] else 0.0
        # Battery correction: +bat_avg reconstructs house load before battery action.
        house_wo_ev = max(0.0, imp_avg + pv_avg + bat_avg - exp_avg - wb_avg)
        out.append([int(b * 1000), round(house_wo_ev, 1)])
    return out


def as_float(v, default=None):
    if v in (None, "unknown", "unavailable", "none", ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def net_no_battery_with_ev_w(grid_import_w, grid_export_w, bat_in_out_w):
    return float(grid_import_w) - float(grid_export_w) + float(bat_in_out_w)


def net_no_battery_no_ev_w(grid_import_w, grid_export_w, bat_in_out_w, wallbox_w):
    return net_no_battery_with_ev_w(grid_import_w, grid_export_w, bat_in_out_w) - float(
        wallbox_w
    )


def real_charge_follow_surplus_w(grid_import_w, grid_export_w, bat_in_out_w):
    return max(
        0.0, -net_no_battery_with_ev_w(grid_import_w, grid_export_w, bat_in_out_w)
    )


def normalize_sample(sample):
    if not isinstance(sample, dict):
        return sample
    gi = float(sample.get("grid_import_w", 0.0) or 0.0)
    ge = float(sample.get("grid_export_w", 0.0) or 0.0)
    pv_w = float(sample.get("pv_w", 0.0) or 0.0)
    house_total_w = max(0.0, float(sample.get("house_total_w", gi + pv_w - ge) or 0.0))
    wb = float(sample.get("wallbox_w", 0.0) or 0.0)
    house_w = max(0.0, house_total_w - wb)
    sample["wallbox_w"] = wb
    sample["house_total_w"] = house_total_w
    sample["house_w"] = house_w
    sample["load_w"] = house_w
    return sample


def migrate_state_sample_v9(sample):
    """Convert the pre-v9 unitless EV sample field to canonical watts once."""
    migrated = dict(sample)
    wallbox_w = float(migrated.get("wallbox_w", 0.0) or 0.0)
    if 0.0 < wallbox_w < 50.0:
        migrated["wallbox_w"] = wallbox_w * 1000.0
    return normalize_sample(migrated)


def normalize_samples(samples):
    if not isinstance(samples, list):
        return []
    return [normalize_sample(dict(s)) for s in samples if isinstance(s, dict)]

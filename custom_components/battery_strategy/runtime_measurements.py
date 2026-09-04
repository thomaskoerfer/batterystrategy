"""Normalized measurements and bounded history views for planning."""

from __future__ import annotations

from .planning_runtime import HistoryRole


def fetch_net_actual_profile(runtime, hours=48):
    cutoff_ms = runtime.captured_at_ms - hours * 3_600_000
    series_map = runtime.history.read(
        [
            HistoryRole.GRID_IMPORT_POWER_W,
            HistoryRole.GRID_EXPORT_POWER_W,
        ],
        cutoff_ms,
    )
    imp = series_map[HistoryRole.GRID_IMPORT_POWER_W]
    exp = series_map[HistoryRole.GRID_EXPORT_POWER_W]
    buckets = {}
    for ts, v in imp:
        h = int(ts // 3_600_000) * 3_600_000
        buckets.setdefault(h, {"imp": [], "exp": []})["imp"].append(float(v))
    for ts, v in exp:
        h = int(ts // 3_600_000) * 3_600_000
        buckets.setdefault(h, {"imp": [], "exp": []})["exp"].append(float(v))
    out = []
    for h in sorted(buckets.keys()):
        rec = buckets[h]
        imp_avg = (sum(rec["imp"]) / len(rec["imp"])) if rec["imp"] else 0.0
        exp_avg = (sum(rec["exp"]) / len(rec["exp"])) if rec["exp"] else 0.0
        out.append([h, round(imp_avg - exp_avg, 1)])
    return out


def fetch_pv_actual_profile(runtime, hours=48):
    cutoff_ms = runtime.captured_at_ms - hours * 3_600_000
    pv = runtime.history.read([HistoryRole.PV_GENERATION_POWER_W], cutoff_ms)[
        HistoryRole.PV_GENERATION_POWER_W
    ]
    buckets = {}
    for ts, v in pv:
        h = int(ts // 3_600_000) * 3_600_000
        buckets.setdefault(h, []).append(max(0.0, float(v)))
    out = []
    for h in sorted(buckets.keys()):
        vals = buckets[h]
        avg = (sum(vals) / len(vals)) if vals else 0.0
        out.append([h, round(avg, 1)])
    return out


def build_house_actual_profile_from_samples(samples, hours=48, *, now_ts):
    now_ts = float(now_ts)
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
    sample_profile = build_house_actual_profile_from_samples(
        samples, hours, now_ts=runtime.captured_at_s
    )
    if len(sample_profile) >= 3:
        return sample_profile

    cutoff_ms = runtime.captured_at_ms - hours * 3_600_000
    series_map = runtime.history.read(
        [
            HistoryRole.GRID_IMPORT_POWER_W,
            HistoryRole.GRID_EXPORT_POWER_W,
            HistoryRole.PV_GENERATION_POWER_W,
            HistoryRole.EV_CHARGE_POWER_W,
            HistoryRole.BATTERY_CHARGE_POWER_W,
            HistoryRole.BATTERY_DISCHARGE_POWER_W,
        ],
        cutoff_ms,
    )
    imp = series_map[HistoryRole.GRID_IMPORT_POWER_W]
    exp = series_map[HistoryRole.GRID_EXPORT_POWER_W]
    pv = series_map[HistoryRole.PV_GENERATION_POWER_W]
    wallbox = series_map[HistoryRole.EV_CHARGE_POWER_W]
    charge = series_map[HistoryRole.BATTERY_CHARGE_POWER_W]
    discharge = series_map[HistoryRole.BATTERY_DISCHARGE_POWER_W]
    buckets = {}

    def bucket(timestamp):
        key = int(timestamp // 900_000) * 900_000
        return key, buckets.setdefault(
            key,
            {
                "imp": [],
                "exp": [],
                "pv": [],
                "wb": [],
                "charge": [],
                "discharge": [],
            },
        )

    for ts, v in imp:
        _, record = bucket(ts)
        record["imp"].append(float(v))
    for ts, v in exp:
        _, record = bucket(ts)
        record["exp"].append(float(v))
    for ts, v in pv:
        _, record = bucket(ts)
        record["pv"].append(max(0.0, float(v)))
    for ts, v in wallbox:
        _, record = bucket(ts)
        record["wb"].append(max(0.0, float(v)))
    for ts, v in charge:
        _, record = bucket(ts)
        record["charge"].append(float(v))
    for ts, v in discharge:
        _, record = bucket(ts)
        record["discharge"].append(float(v))
    out = []
    for b in sorted(buckets.keys()):
        rec = buckets[b]
        imp_avg = (sum(rec["imp"]) / len(rec["imp"])) if rec["imp"] else 0.0
        exp_avg = (sum(rec["exp"]) / len(rec["exp"])) if rec["exp"] else 0.0
        pv_avg = (sum(rec["pv"]) / len(rec["pv"])) if rec["pv"] else 0.0
        wb_avg = (sum(rec["wb"]) / len(rec["wb"])) if rec["wb"] else 0.0
        charge_avg = sum(rec["charge"]) / len(rec["charge"]) if rec["charge"] else 0.0
        discharge_avg = (
            sum(rec["discharge"]) / len(rec["discharge"]) if rec["discharge"] else 0.0
        )
        bat_avg = discharge_avg - charge_avg
        # Battery correction: +bat_avg reconstructs house load before battery action.
        house_wo_ev = max(0.0, imp_avg + pv_avg + bat_avg - exp_avg - wb_avg)
        out.append([b, round(house_wo_ev, 1)])
    return out


def as_float(v, default=None):
    if v in (None, "unknown", "unavailable", "none", ""):
        return default
    try:
        return float(v)
    except TypeError, ValueError:
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

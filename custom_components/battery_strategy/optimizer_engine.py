#!/usr/bin/env python3
"""Transitional optimizer monolith; see ARCHITECTURE.md before changing layers."""

import base64
import bisect
import datetime as dt
import json
import math
import os
import statistics
import time
import urllib.parse
from functools import lru_cache
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .contracts import (
    ForecastRequest,
    LoadDriverSnapshot,
    LoadForecastContext,
    SlotKey,
)
from .forecasting import (
    LegacyForecastConfig,
    LegacyForecastSample,
    LegacyForecastTarget,
    build_legacy_forecast,
    evaluate_feature_store_shadow,
)
from .optimizer_state import load_state_document, save_state_document

STATE_FILE = "/config/battery_strategy_optimizer_state.json"
SCRIPT_VERSION = "1.8.14"
_DB_ENGINE = None
_RUNTIME_STATES = {}
_RUNTIME_PRICE_INTERVALS = []
_ENTITY_MAP = {}
_ENTITY_SCALE = {}
_SHADOW_FEATURE_HISTORY = ()

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

ETA_RT = 0.80
ETA_C = ETA_RT ** 0.5
ETA_D = ETA_RT ** 0.5
CAP_KWH = 6.0
SOC_MIN = 10.0
SOC_MAX = 100.0
MIN_E_KWH = CAP_KWH * (SOC_MIN / 100.0)
MAX_E_KWH = CAP_KWH * (SOC_MAX / 100.0)
MAX_P_W = 2400
SLOT_H = 0.25
MAX_E_SLOT_KWH = (MAX_P_W / 1000.0) * SLOT_H
MAX_CHARGE_P_W = 2400
MAX_DISCHARGE_P_W = 2400
MAX_CHARGE_E_SLOT_KWH = (MAX_CHARGE_P_W / 1000.0) * SLOT_H
MAX_DISCHARGE_E_SLOT_KWH = (MAX_DISCHARGE_P_W / 1000.0) * SLOT_H
PV_CHARGING_ENABLED = True
# Direct engine callers retain the historical full-optimizer defaults. The HA
# adapter always supplies the configured policy before a production run.
GRID_CHARGING_ENABLED = True
DISCHARGE_ENABLED = True
PLANNING_HORIZON_H = 48
ENERGY_STEP_KWH = 0.025
MIN_MARGIN_CT = 2.0
HISTORY_DAYS = 60
ACTUAL_SAVINGS_DAYS = 21
BIAS_ALPHA = 0.12
SLOT_BIAS_ALPHA = 0.08
SLOTS_PER_DAY = 96
SWITCH_PENALTY_MIN = 5.0
SWITCH_PENALTY_REF_W = 500.0
TRACE_MIN_INTERVAL_S = 240
TRACE_RETENTION_DAYS = 14
TRACE_MAX_POINTS = 8000
EEX_SCOPE_URL = "https://api.eex-group.com/pub/customise-widget/filter-data-with-scope"
EEX_TABLE_URL = "https://api.eex-group.com/pub/market-data/table-data"
EEX_CACHE_TTL_S = 6 * 3600
OPEN_METEO_TZ = ZoneInfo("Europe/Berlin")
TERMINAL_RANK_THRESHOLD = 0.35
TERMINAL_VALUE_CAP_CT = 25.0
CHEAP_CHARGE_RANK_THRESHOLD = 0.35
CHEAP_CHARGE_BONUS_CAP_CT = 12.0
MICROCYCLE_LOOKBACK_SLOTS = 8
CHARGE_DEFERRAL_MARGIN_CT = 0.5
PV_RECOVERY_LOOKAHEAD_H = 18.0
PV_RECOVERY_CONFIDENCE = 0.75
PV_RECOVERY_RESERVE_KWH = 0.30
PV_EXPORT_OPPORTUNITY_CT = 0.0
SCARCE_VALUE_TIE_CT = 0.5
EEX_PROXY_MIN_FULL_DAY_SLOTS = 90
EEX_PROXY_RECENT_DAYS = 5
EEX_PROXY_MIN_RETAIL_MARKUP_CT = 18.0
EEX_PROXY_MAX_BASE_RETAIL_MARKUP_CT = 28.0
EEX_PROXY_MAX_PEAK_RETAIL_MARKUP_CT = 32.0
EEX_PROXY_MIN_PRICE_CT = 12.0
EEX_PROXY_MAX_PRICE_CT = 70.0
PV_CAPACITY_EVENTS = [
    ("2000-01-01T00:00:00+00:00", 1.0, 1.0),
]

# PV surplus anti-cycling thresholds
PV_SURPLUS_START_AVG_W = 50.0
PV_SURPLUS_MIN_SAMPLE_W = 40.0
PV_SURPLUS_REQUIRED_COUNT = 1
PV_SURPLUS_WINDOW_SAMPLES = 1

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=0&longitude=0"
    "&current=cloud_cover,shortwave_radiation"
    "&hourly=cloud_cover,shortwave_radiation"
    "&forecast_days=3&timezone=Europe%2FBerlin"
)


def configure_runtime(context):
    """Apply one config-entry runtime context before an optimizer run."""
    global STATE_FILE, _DB_ENGINE
    global _RUNTIME_STATES, _RUNTIME_PRICE_INTERVALS, _ENTITY_MAP, _ENTITY_SCALE
    global _SHADOW_FEATURE_HISTORY
    global CAP_KWH, SOC_MIN, SOC_MAX, MIN_E_KWH, MAX_E_KWH, MAX_P_W, MAX_E_SLOT_KWH
    global MAX_CHARGE_P_W, MAX_DISCHARGE_P_W, MAX_CHARGE_E_SLOT_KWH, MAX_DISCHARGE_E_SLOT_KWH
    global PV_CHARGING_ENABLED, GRID_CHARGING_ENABLED, DISCHARGE_ENABLED, PLANNING_HORIZON_H
    global ETA_RT, ETA_C, ETA_D, MIN_MARGIN_CT, PV_EXPORT_OPPORTUNITY_CT
    global OPEN_METEO_TZ, OPEN_METEO_URL, PV_CAPACITY_EVENTS

    config_dir = str(context.get("config_dir") or "/config")
    STATE_FILE = os.path.join(config_dir, "battery_strategy_optimizer_state.json")
    _DB_ENGINE = context.get("db_engine")
    _RUNTIME_STATES = dict(context.get("states") or {})
    _RUNTIME_PRICE_INTERVALS = list(context.get("price_intervals") or [])
    _ENTITY_MAP = {key: value for key, value in (context.get("entity_map") or {}).items() if value}
    _ENTITY_SCALE = {
        key: float(value)
        for key, value in (context.get("entity_scale") or {}).items()
        if value is not None
    }
    _SHADOW_FEATURE_HISTORY = tuple(context.get("shadow_feature_history") or ())

    CAP_KWH = max(0.5, float(context.get("battery_capacity_kwh") or 6.0))
    SOC_MIN = max(0.0, min(100.0, float(context.get("min_soc_pct") or 0.0)))
    SOC_MAX = max(SOC_MIN, min(100.0, float(context.get("max_soc_pct") or 100.0)))
    MIN_E_KWH = CAP_KWH * SOC_MIN / 100.0
    MAX_E_KWH = CAP_KWH * SOC_MAX / 100.0
    MAX_CHARGE_P_W = max(0.0, float(context.get("max_charge_power_w") or context.get("max_power_w") or 2400.0))
    MAX_DISCHARGE_P_W = max(0.0, float(context.get("max_discharge_power_w") or context.get("max_power_w") or 2400.0))
    MAX_P_W = max(MAX_CHARGE_P_W, MAX_DISCHARGE_P_W)
    MAX_E_SLOT_KWH = (MAX_P_W / 1000.0) * SLOT_H
    MAX_CHARGE_E_SLOT_KWH = (MAX_CHARGE_P_W / 1000.0) * SLOT_H
    MAX_DISCHARGE_E_SLOT_KWH = (MAX_DISCHARGE_P_W / 1000.0) * SLOT_H
    PV_CHARGING_ENABLED = str(context.get("pv_charging") or "on") != "off"
    GRID_CHARGING_ENABLED = str(context.get("grid_charging") or "off") != "off"
    DISCHARGE_ENABLED = str(context.get("discharge") or "load") != "off"
    PLANNING_HORIZON_H = max(1, min(48, int(context.get("planning_horizon_h") or 48)))
    ETA_RT = max(0.01, min(1.0, float(context.get("round_trip_efficiency") or 0.8)))
    ETA_C = ETA_RT ** 0.5
    ETA_D = ETA_RT ** 0.5
    MIN_MARGIN_CT = max(0.0, float(context.get("min_margin_ct_per_kwh", 2.0)))
    PV_EXPORT_OPPORTUNITY_CT = max(0.0, float(context.get("feed_in_tariff_ct_per_kwh", 0.0)))

    timezone = str(context.get("timezone") or "UTC")
    OPEN_METEO_TZ = ZoneInfo(timezone)
    latitude = float(context.get("latitude") or 0.0)
    longitude = float(context.get("longitude") or 0.0)
    OPEN_METEO_URL = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&current=cloud_cover,shortwave_radiation"
        "&hourly=cloud_cover,shortwave_radiation"
        f"&forecast_days=3&timezone={urllib.parse.quote(timezone)}"
    )
    capacity_events = context.get("pv_capacity_events") or []
    if capacity_events:
        PV_CAPACITY_EVENTS = [tuple(item) for item in capacity_events]
    else:
        pv_kwp = max(0.1, float(context.get("pv_capacity_kwp") or 1.0))
        inverter_kw = max(0.1, float(context.get("pv_inverter_power_kw") or pv_kwp))
        PV_CAPACITY_EVENTS = [("2000-01-01T00:00:00+00:00", pv_kwp, inverter_kw)]
    # This cache depends on mutable runtime timezone configuration.
    local_dt_from_ts.cache_clear()


def _entity_id(key):
    return _ENTITY_MAP.get(key, key)


def _get_db_engine():
    if _DB_ENGINE is None:
        raise RuntimeError("Home Assistant recorder engine is unavailable")
    return _DB_ENGINE


def _query_latest_state_sql(entity_id):
    entity_id = _entity_id(entity_id)
    q = text(
        """
        SELECT s.state
        FROM states s
        JOIN states_meta sm ON sm.metadata_id = s.metadata_id
        WHERE sm.entity_id = :entity_id
        ORDER BY s.state_id DESC
        LIMIT 1
        """
    )
    with _get_db_engine().connect() as conn:
        row = conn.execute(q, {"entity_id": entity_id}).fetchone()
    return row[0] if row else None


def _build_in_params(values, prefix):
    params = {}
    placeholders = []
    for idx, value in enumerate(values):
        key = f"{prefix}{idx}"
        params[key] = value
        placeholders.append(f":{key}")
    return ", ".join(placeholders), params


def _query_latest_states_sql(entity_ids):
    if not entity_ids:
        return {}
    requested = list(entity_ids)
    actual = [_entity_id(entity_id) for entity_id in requested]
    in_sql, params = _build_in_params(actual, "eid")
    q = text(
        f"""
        SELECT sm.entity_id, s.state
        FROM states s
        JOIN states_meta sm ON sm.metadata_id = s.metadata_id
        JOIN (
            SELECT sm2.entity_id, MAX(s2.state_id) AS max_state_id
            FROM states s2
            JOIN states_meta sm2 ON sm2.metadata_id = s2.metadata_id
            WHERE sm2.entity_id IN ({in_sql})
            GROUP BY sm2.entity_id
        ) latest ON latest.max_state_id = s.state_id
        """
    )
    with _get_db_engine().connect() as conn:
        rows = conn.execute(q, params).fetchall()
    by_actual = {row[0]: row[1] for row in rows}
    return {key: by_actual.get(value) for key, value in zip(requested, actual) if value in by_actual}


def _query_series_sql(entity_id, cutoff_ts):
    entity_id = _entity_id(entity_id)
    q = text(
        """
        SELECT s.last_updated_ts, s.state
        FROM states s
        JOIN states_meta sm ON sm.metadata_id = s.metadata_id
        WHERE sm.entity_id = :entity_id AND s.last_updated_ts >= :cutoff_ts
        ORDER BY s.last_updated_ts
        """
    )
    with _get_db_engine().connect() as conn:
        rows = conn.execute(q, {"entity_id": entity_id, "cutoff_ts": float(cutoff_ts)}).fetchall()
    return rows


def _query_series_many_sql(entity_ids, cutoff_ts):
    if not entity_ids:
        return {}
    requested = list(entity_ids)
    actual = [_entity_id(entity_id) for entity_id in requested]
    in_sql, params = _build_in_params(actual, "eid")
    params["cutoff_ts"] = float(cutoff_ts)
    q = text(
        f"""
        SELECT sm.entity_id, s.last_updated_ts, s.state
        FROM states s
        JOIN states_meta sm ON sm.metadata_id = s.metadata_id
        WHERE sm.entity_id IN ({in_sql}) AND s.last_updated_ts >= :cutoff_ts
        ORDER BY sm.entity_id, s.last_updated_ts
        """
    )
    with _get_db_engine().connect() as conn:
        rows = conn.execute(q, params).fetchall()
    grouped_actual = {eid: [] for eid in actual}
    for entity_id, ts, state in rows:
        grouped_actual.setdefault(entity_id, []).append((ts, state))
    return {key: grouped_actual.get(value, []) for key, value in zip(requested, actual)}


def get_latest_states(entity_ids):
    out = {}
    try:
        out.update(_query_latest_states_sql(entity_ids))
    except Exception:
        pass
    out.update({key: _RUNTIME_STATES[key] for key in entity_ids if key in _RUNTIME_STATES})
    for eid in entity_ids:
        if eid in out:
            continue
        try:
            out[eid] = _query_latest_state_sql(eid)
        except Exception:
            out[eid] = None
    return out


def fetch_sensor_series_many(entity_ids, cutoff_ts):
    parsed = {}
    remaining = list(entity_ids)
    try:
        rows_map = _query_series_many_sql(entity_ids, cutoff_ts)
        remaining = []
        for entity_id in entity_ids:
            out = []
            for ts, st in rows_map.get(entity_id, []):
                try:
                    out.append((float(ts), float(st) * _ENTITY_SCALE.get(entity_id, 1.0)))
                except Exception:
                    continue
            parsed[entity_id] = out
    except Exception:
        pass

    for entity_id in remaining:
        parsed[entity_id] = fetch_sensor_series(entity_id, cutoff_ts)
    return parsed


def fetch_sensor_series(entity_id, cutoff_ts):
    try:
        rows = _query_series_sql(entity_id, cutoff_ts)
    except Exception:
        rows = []
    out = []
    for ts, st in rows:
        try:
            v = float(st) * _ENTITY_SCALE.get(entity_id, 1.0)
            out.append((float(ts), v))
        except Exception:
            continue
    return out


def fetch_net_actual_profile(hours=48):
    cutoff_ts = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    series_map = fetch_sensor_series_many(
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


def fetch_pv_actual_profile(hours=48):
    cutoff_ts = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    pv = fetch_sensor_series_many([E_PV_POWER], cutoff_ts)[E_PV_POWER]
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
    now_ts = float(now_ts if now_ts is not None else dt.datetime.now(dt.timezone.utc).timestamp())
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


def fetch_house_actual_profile(hours=48, samples=None):
    sample_profile = build_house_actual_profile_from_samples(samples, hours)
    if len(sample_profile) >= 3:
        return sample_profile

    cutoff_ts = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    series_map = fetch_sensor_series_many(
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
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})["imp"].append(float(v))
    for ts, v in exp:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})["exp"].append(float(v))
    for ts, v in pv:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})["pv"].append(max(0.0, float(v)))
    for ts, v in wallbox:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})["wb"].append(max(0.0, float(v) * 1000.0))
    for ts, v in bat:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {"imp": [], "exp": [], "pv": [], "wb": [], "bat": []})["bat"].append(float(v))
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
    except Exception:
        return default


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def net_no_battery_with_ev_w(grid_import_w, grid_export_w, bat_in_out_w):
    return float(grid_import_w) - float(grid_export_w) + float(bat_in_out_w)


def net_no_battery_no_ev_w(grid_import_w, grid_export_w, bat_in_out_w, wallbox_w):
    return net_no_battery_with_ev_w(grid_import_w, grid_export_w, bat_in_out_w) - float(wallbox_w)


def real_charge_follow_surplus_w(grid_import_w, grid_export_w, bat_in_out_w):
    return max(0.0, -net_no_battery_with_ev_w(grid_import_w, grid_export_w, bat_in_out_w))


def med(vals, fallback):
    if not vals:
        return fallback
    return float(statistics.median(vals))


def sample_has_valid_live_power(sample):
    gi = float(sample.get("grid_import_w", 0.0) or 0.0)
    ge = float(sample.get("grid_export_w", 0.0) or 0.0)
    pv = float(sample.get("pv_w", 0.0) or 0.0)
    lw = float(sample.get("load_w", 0.0) or 0.0)
    return not (lw <= 1.0 and gi <= 1.0 and ge <= 1.0 and pv <= 1.0)


def normalize_sample(sample):
    if not isinstance(sample, dict):
        return sample
    gi = float(sample.get("grid_import_w", 0.0) or 0.0)
    ge = float(sample.get("grid_export_w", 0.0) or 0.0)
    pv_w = float(sample.get("pv_w", 0.0) or 0.0)
    house_total_w = max(0.0, float(sample.get("house_total_w", gi + pv_w - ge) or 0.0))
    wb = float(sample.get("wallbox_w", 0.0) or 0.0)
    # Historical compatibility case: wallbox sensor is kW, but older state samples stored it as if it were W.
    if 0.0 < wb < 50.0:
        wb *= 1000.0
    house_w = max(0.0, house_total_w - wb)
    sample["wallbox_w"] = wb
    sample["house_total_w"] = house_total_w
    sample["house_w"] = house_w
    sample["load_w"] = house_w  # backward compatible forecast key
    return sample


def normalize_samples(samples):
    if not isinstance(samples, list):
        return []
    return [normalize_sample(dict(s)) for s in samples if isinstance(s, dict)]


def eex_headers():
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.eex.com/",
        "Origin": "https://www.eex.com",
        "Accept": "application/json, text/plain, */*",
    }


def fetch_json(url, data=None, headers=None):
    hdrs = headers or {}
    req = Request(url, data=data, headers=hdrs)
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def eex_filter_rows(product):
    payload = [
        {
            "commodity": "POWER",
            "pricing": "F",
            "area": "DE",
            "product": product,
            "productSpecific": "All",
            "maturityType": "Day",
        }
    ]
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    query = urllib.parse.urlencode({"data": encoded})
    url = f"{EEX_SCOPE_URL}?{query}"
    body = query.encode("utf-8")
    headers = dict(eex_headers())
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    obj = fetch_json(url, data=body, headers=headers)
    header = obj.get("header", [])
    rows = []
    for row in obj.get("data", []):
        rec = dict(zip(header, row))
        y = rec.get("displayYear")
        m = rec.get("displayMonth")
        d = rec.get("displayDay")
        sc = rec.get("shortCode")
        maturity = rec.get("maturity")
        if y and m and d and sc and maturity:
            rows.append(
                {
                    "delivery_date": f"{int(y):04d}-{int(m):02d}-{int(d):02d}",
                    "shortCode": sc,
                    "maturity": str(maturity),
                    "product": product,
                }
            )
    return rows


def eex_fetch_settlement(row, trade_date):
    params = {
        "shortCode": row["shortCode"],
        "commodity": "POWER",
        "pricing": "F",
        "area": "DE",
        "product": row["product"],
        "maturity": row["maturity"],
        "startDate": trade_date,
        "endDate": trade_date,
        "maturityType": "Day",
        "isRolling": "true",
    }
    url = f"{EEX_TABLE_URL}?{urllib.parse.urlencode(params)}"
    obj = fetch_json(url, headers=eex_headers())
    header = obj.get("header", [])
    for data_row in obj.get("data", []):
        rec = dict(zip(header, data_row))
        px = rec.get("settlPx")
        if px is not None:
            return {
                "trade_date": rec.get("tradeDate", trade_date),
                "settl_eur_mwh": float(px),
                "settl_ct_kwh": round(float(px) / 10.0, 3),
            }
    return None


def get_eex_day_context(data, local_now):
    cache = data.setdefault("eex_cache", {})
    fetched_at = float(cache.get("fetched_at_ts", 0.0) or 0.0)
    if cache.get("days") and (local_now.timestamp() - fetched_at) < EEX_CACHE_TTL_S:
        return cache["days"]

    target_dates = [(local_now.date() + dt.timedelta(days=i)).isoformat() for i in range(0, 4)]
    out = {d: {} for d in target_dates}
    try:
        base_rows = {r["delivery_date"]: r for r in eex_filter_rows("Base")}
        peak_rows = {r["delivery_date"]: r for r in eex_filter_rows("Peak")}
        # Use the last completed trading day and walk backwards until EEX returns data.
        trade_candidates = [(local_now.date() - dt.timedelta(days=i)).isoformat() for i in range(1, 8)]
        for delivery_date in target_dates:
            for product, rows in (("base", base_rows), ("peak", peak_rows)):
                row = rows.get(delivery_date)
                if not row:
                    continue
                settlement = None
                for trade_date in trade_candidates:
                    settlement = eex_fetch_settlement(row, trade_date)
                    if settlement:
                        break
                if settlement:
                    out[delivery_date][product] = settlement
        for delivery_date, rec in out.items():
            if rec.get("base") and rec.get("peak"):
                rec["spread_ct_kwh"] = round(rec["peak"]["settl_ct_kwh"] - rec["base"]["settl_ct_kwh"], 3)
    except Exception:
        pass

    cache["fetched_at_ts"] = local_now.timestamp()
    cache["days"] = out
    return out


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def compute_price_quantiles(samples, local_now, current_price_ct, tomorrow_prices):
    slot = slot_index_for_dt(local_now)
    wd = local_now.weekday()
    vals = []
    for s in samples:
        ts = s.get("ts")
        price = s.get("price_ct")
        if ts is None or price in (None, 0):
            continue
        d = local_dt_from_ts(ts).astimezone(local_now.tzinfo)
        if d.weekday() == wd and slot_index_for_dt(d) == slot:
            vals.append(float(price))
    vals = sorted(vals)
    median = quantile(vals, 0.5)
    q20 = quantile(vals, 0.2)
    q80 = quantile(vals, 0.8)
    rank = None
    if vals and current_price_ct is not None:
        below = sum(1 for v in vals if v <= current_price_ct)
        rank = below / len(vals)

    tomorrow_vals = sorted(v for _, v in tomorrow_prices) if tomorrow_prices else []
    tomorrow_min = min(tomorrow_vals) if tomorrow_vals else None
    tomorrow_min_rank = None
    day_slot_vals = []
    if tomorrow_prices:
        tomorrow_wd = (local_now.date() + dt.timedelta(days=1)).weekday()
        for s in samples:
            ts = s.get("ts")
            price = s.get("price_ct")
            if ts is None or price in (None, 0):
                continue
            d = local_dt_from_ts(ts).astimezone(local_now.tzinfo)
            if d.weekday() == tomorrow_wd:
                day_slot_vals.append(float(price))
        day_slot_vals.sort()
        if day_slot_vals and tomorrow_min is not None:
            below = sum(1 for v in day_slot_vals if v <= tomorrow_min)
            tomorrow_min_rank = below / len(day_slot_vals)

    return {
        "current_slot_median_ct": round(median, 3) if median is not None else None,
        "current_slot_q20_ct": round(q20, 3) if q20 is not None else None,
        "current_slot_q80_ct": round(q80, 3) if q80 is not None else None,
        "current_slot_rank": round(rank, 3) if rank is not None else None,
        "tomorrow_min_price_ct": round(tomorrow_min, 3) if tomorrow_min is not None else None,
        "tomorrow_min_rank": round(tomorrow_min_rank, 3) if tomorrow_min_rank is not None else None,
    }


def compute_weekday_price_rank(samples, target_date, price_value):
    if price_value is None:
        return None
    target_wd = target_date.weekday()
    vals = []
    for s in samples:
        ts = s.get("ts")
        price = s.get("price_ct")
        if ts is None or price in (None, 0):
            continue
        d = local_dt_from_ts(ts).date()
        if d.weekday() == target_wd:
            vals.append(float(price))
    if not vals:
        return None
    vals.sort()
    below = sum(1 for v in vals if v <= price_value)
    return below / len(vals)


def compute_weekday_price_quantile(samples, target_date, q):
    target_wd = target_date.weekday()
    vals = []
    for s in samples:
        ts = s.get("ts")
        price = s.get("price_ct")
        if ts is None or price in (None, 0):
            continue
        d = local_dt_from_ts(ts).date()
        if d.weekday() == target_wd:
            vals.append(float(price))
    vals.sort()
    return quantile(vals, q)


def load_state():
    default_state = {
        "samples": [],
        "predictions": [],
        "backtests": [],
        "pv_bias": 1.0,
        "load_bias": 1.0,
        "pv_bias_slots": [1.0] * SLOTS_PER_DAY,
        "load_bias_slots": [1.0] * SLOTS_PER_DAY,
        "virtual_energy_kwh": CAP_KWH * 0.5,
        "virtual_last_ts": None,
        "virtual_last_mode": "idle",
        "virtual_last_power_w": 0.0,
        "virtual_trace": [],
        "last_known_soc_pct": None,
        "eex_cache": {},
        "daily_savings": {},
        "actual_daily_savings": {},
        "last_output": {},
        "state_schema": 8,
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        data = load_state_document(STATE_FILE)
        if data is None:
            return default_state
        for k, v in default_state.items():
            data.setdefault(k, v)
        # Phase-1 comparison traces are obsolete once the extracted forecast is
        # the sole production path. Drop both historical names during migration.
        data.pop("forecast_shadow_trace", None)
        data.pop("forecast_parity_trace", None)
        if int(data.get("state_schema", 0)) < 4:
            data["virtual_energy_kwh"] = CAP_KWH * 0.5
            data["virtual_last_ts"] = None
            data["virtual_last_mode"] = "idle"
            data["virtual_last_power_w"] = 0.0
            data["virtual_trace"] = []
        data["samples"] = normalize_samples(data.get("samples", []))
        data["state_schema"] = 8
        data["virtual_trace"] = compact_virtual_trace(data.get("virtual_trace", []))
        return data
    except Exception:
        return default_state


def fallback_output(mode, reason, data, now_iso):
    last = data.get("last_output") if isinstance(data, dict) else {}
    if not isinstance(last, dict):
        last = {}
    out = dict(last)
    out["mode"] = mode
    out["reason"] = reason
    out["script_version"] = SCRIPT_VERSION
    out["timestamp"] = now_iso
    return out


def save_state(data):
    save_state_document(STATE_FILE, data)


def normalize_slot_biases(arr, lo, hi):
    if not isinstance(arr, list) or len(arr) != SLOTS_PER_DAY:
        return [1.0] * SLOTS_PER_DAY
    out = []
    for v in arr:
        try:
            out.append(clamp(float(v), lo, hi))
        except Exception:
            out.append(1.0)
    return out


def compact_virtual_trace(trace):
    if not isinstance(trace, list):
        return []
    out = []
    min_delta_ms = TRACE_MIN_INTERVAL_S * 1000
    for item in sorted(trace, key=lambda x: x.get("ts_ms", 0)):
        ts_ms = int(item.get("ts_ms", 0))
        if out and ts_ms - int(out[-1].get("ts_ms", 0)) < min_delta_ms:
            out[-1] = item
        else:
            out.append(item)
    return out


def slot_index_for_dt(dt_obj):
    return dt_obj.hour * 4 + dt_obj.minute // 15


@lru_cache(maxsize=131072)
def local_dt_from_ts(ts):
    return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).astimezone(OPEN_METEO_TZ)


def bootstrap_samples_from_db(now_ts, days=21):
    cutoff = now_ts - days * 86400
    series_map = fetch_sensor_series_many(
        [
            E_GRID_IMPORT,
            E_GRID_EXPORT,
            E_PV_POWER,
            E_EV_POWER,
            E_BATTERY_POWER,
            E_HEAT_PUMP_POWER,
            E_PRICE_CURRENT,
        ],
        cutoff,
    )
    grid_import = series_map[E_GRID_IMPORT]
    grid_export = series_map[E_GRID_EXPORT]
    pv = series_map[E_PV_POWER]
    wallbox = series_map[E_EV_POWER]
    bat = series_map[E_BATTERY_POWER]
    hp = series_map[E_HEAT_PUMP_POWER]
    price = series_map[E_PRICE_CURRENT]

    buckets = {}
    for ts, val in grid_import:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {})["grid_import_w"] = val
    for ts, val in grid_export:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {})["grid_export_w"] = val
    for ts, val in pv:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {})["pv_w"] = val
    for ts, val in wallbox:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {})["wallbox_w"] = val
    for ts, val in bat:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {})["bat_in_out_w"] = val
    for ts, val in hp:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {})["hp_w"] = val
    for ts, val in price:
        b = int(ts // 900) * 900
        buckets.setdefault(b, {})["price_ct"] = val

    samples = []
    for b in sorted(buckets.keys()):
        rec = buckets[b]
        if "grid_import_w" not in rec and "pv_w" not in rec and "grid_export_w" not in rec:
            continue
        gi = float(rec.get("grid_import_w", 0.0))
        ge = float(rec.get("grid_export_w", 0.0))
        pv_w = float(rec.get("pv_w", 0.0))
        wallbox_w = max(0.0, float(rec.get("wallbox_w", 0.0)))
        bat_w = float(rec.get("bat_in_out_w", 0.0))
        # Reconstruct pre-battery house power from meter values.
        house_w_total = max(0.0, gi + pv_w + bat_w - ge)
        house_w = max(0.0, house_w_total - wallbox_w)
        samples.append(
            {
                "ts": float(b),
                "load_w": house_w,  # backward compatible key: represents house load
                "house_w": house_w,
                "house_total_w": house_w_total,
                "wallbox_w": wallbox_w,
                "grid_import_w": gi,
                "grid_export_w": ge,
                "pv_w": pv_w,
                "bat_in_out_w": bat_w,
                "hp_w": float(rec.get("hp_w", 0.0)),
                "price_ct": float(rec.get("price_ct", 0.0)),
                "soc": -1,
            }
        )
    return samples[-12000:]


def avg_power(samples, start_ts, end_ts, key):
    vals = [s.get(key, 0.0) for s in samples if start_ts <= s.get("ts", 0) <= end_ts]
    return (sum(vals) / len(vals)) if vals else None


def slot_idx_for_ts(ts):
    return slot_index_for_dt(local_dt_from_ts(ts))


def forecast_load_w_for_slot(samples, target_dt, fallback_w, hp_now_w, now_dt=None, load_bias=1.0, slot_bias=1.0):
    target_slot = target_dt.hour * 4 + target_dt.minute // 15
    target_wd = target_dt.weekday()
    target_is_weekend = target_wd >= 5

    same_slot = []
    same_slot_wd = []
    same_slot_weektype = []
    recent = []

    for s in samples[-6000:]:
        ts = s.get("ts", 0)
        dt_s = local_dt_from_ts(ts).astimezone(target_dt.tzinfo)
        slot = slot_idx_for_ts(ts)
        lw = float(s.get("load_w", fallback_w))
        if not sample_has_valid_live_power(s):
            continue
        if slot == target_slot:
            same_slot.append(lw)
            if dt_s.weekday() == target_wd:
                same_slot_wd.append(lw)
            if (dt_s.weekday() >= 5) == target_is_weekend:
                same_slot_weektype.append(lw)
        if ts >= (samples[-1]["ts"] - 7200):
            recent.append(lw)

    base_all = med(same_slot[-60:], fallback_w)
    base_wd = med(same_slot_wd[-20:], base_all)
    base_weektype = med(same_slot_weektype[-30:], base_all)
    trend = (sum(recent) / len(recent)) if recent else base_all

    load_w = 0.45 * base_wd + 0.25 * base_weektype + 0.15 * base_all + 0.15 * trend

    # Near-term heatpump boost decays with forecast horizon to avoid flattening day-ahead shape.
    if hp_now_w is not None and now_dt is not None:
        horizon_h = max(0.0, (target_dt - now_dt).total_seconds() / 3600.0)
        if horizon_h <= 6.0:
            hp_excess = max(0.0, hp_now_w - 500.0)
            load_w += 0.22 * hp_excess * math.exp(-horizon_h / 1.5)

    return max(0.0, load_w * clamp(load_bias, 0.6, 1.6) * clamp(slot_bias, 0.7, 1.4))


def recent_surplus_stable(samples):
    recent = samples[-PV_SURPLUS_WINDOW_SAMPLES:]
    if len(recent) < PV_SURPLUS_WINDOW_SAMPLES:
        return False, 0.0
    surplus_vals = [float(s.get("pv_w", 0.0)) - float(s.get("load_w", 0.0)) for s in recent]
    avg_surplus = sum(surplus_vals) / len(surplus_vals)
    high_count = sum(1 for x in surplus_vals if x > PV_SURPLUS_MIN_SAMPLE_W)
    stable = (avg_surplus > PV_SURPLUS_START_AVG_W) and (high_count >= PV_SURPLUS_REQUIRED_COUNT)
    return stable, avg_surplus


def weather_factor_from_cloud_rad(cloud_cover, shortwave_radiation):
    cloud_factor = clamp(1.0 - float(cloud_cover or 0.0) / 130.0, 0.35, 1.05)
    rad_factor = clamp(float(shortwave_radiation or 0.0) / 650.0, 0.2, 1.1)
    return 0.6 * cloud_factor + 0.4 * rad_factor


def open_meteo_weather():
    try:
        with urlopen(OPEN_METEO_URL, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        cur = payload.get("current", {})
        hourly = payload.get("hourly", {})
        hourly_map = {}
        times = hourly.get("time", []) or []
        clouds = hourly.get("cloud_cover", []) or []
        radiation = hourly.get("shortwave_radiation", []) or []
        for ts, cloud, rad in zip(times, clouds, radiation):
            try:
                dt_obj = dt.datetime.fromisoformat(ts)
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=OPEN_METEO_TZ)
                else:
                    dt_obj = dt_obj.astimezone(OPEN_METEO_TZ)
                hour_key = dt_obj.replace(minute=0, second=0, microsecond=0).isoformat()
                hourly_map[hour_key] = {
                    "cloud_cover": float(cloud),
                    "shortwave_radiation": float(rad),
                    "weather_factor": round(weather_factor_from_cloud_rad(cloud, rad), 4),
                }
            except Exception:
                continue
        return {
            "cloud_cover": float(cur.get("cloud_cover", 50.0)),
            "shortwave_radiation": float(cur.get("shortwave_radiation", 0.0)),
            "weather_factor": round(
                weather_factor_from_cloud_rad(cur.get("cloud_cover", 50.0), cur.get("shortwave_radiation", 0.0)),
                4,
            ),
            "hourly": hourly_map,
        }
    except Exception:
        return None


def read_tibber_future_price_stats(now_local):
    date_set = {
        now_local.date().isoformat(),
        (now_local.date() + dt.timedelta(days=1)).isoformat(),
    }
    intervals = read_tibber_intervals_for_dates(date_set)
    if not intervals:
        return None
    future = [it["price_eur"] * 100.0 for it in intervals if it["dt"] >= now_local]
    if not future:
        return None
    return {"min_ct": min(future), "max_ct": max(future)}


def floor_to_quarter(dt_obj):
    return dt_obj.replace(minute=(dt_obj.minute // 15) * 15, second=0, microsecond=0)


def advance_virtual_energy(data, now_ts):
    energy = clamp(float(data.get("virtual_energy_kwh", CAP_KWH * 0.5)), MIN_E_KWH, MAX_E_KWH)
    last_ts = data.get("virtual_last_ts")
    if not last_ts:
        data["virtual_energy_kwh"] = energy
        return energy

    elapsed_h = max(0.0, (now_ts - float(last_ts)) / 3600.0)
    last_mode = data.get("virtual_last_mode", "idle")
    last_power_w = max(0.0, min(MAX_P_W, float(data.get("virtual_last_power_w", 0.0))))
    e_cmd = (last_power_w / 1000.0) * elapsed_h
    if last_mode in ("charge_grid", "charge_pv_surplus"):
        energy += e_cmd * ETA_C
    elif str(last_mode).startswith("discharge_"):
        energy -= e_cmd / ETA_D
    energy = clamp(energy, MIN_E_KWH, MAX_E_KWH)
    data["virtual_energy_kwh"] = energy
    return energy


def append_virtual_trace(data, ts_ms, date_str, soc_pct, mode, power_w):
    if mode in ("charge_grid", "charge_pv_surplus"):
        charge_w = max(0.0, power_w)
        discharge_w = 0.0
    elif mode.startswith("discharge_"):
        charge_w = 0.0
        discharge_w = max(0.0, power_w)
    else:
        charge_w = 0.0
        discharge_w = 0.0
        power_w = 0.0
    trace = data.setdefault("virtual_trace", [])
    point = {
        "ts_ms": int(ts_ms),
        "date": date_str,
        "soc_pct": round(float(soc_pct), 2),
        "power_w": round(float(power_w if not mode.startswith("discharge_") else -power_w), 1),
        "charge_fc_w": round(charge_w, 1),
        "discharge_fc_w": round(discharge_w, 1),
    }
    if trace and int(ts_ms) - int(trace[-1].get("ts_ms", 0)) < TRACE_MIN_INTERVAL_S * 1000:
        trace[-1] = point
    else:
        trace.append(point)
    cutoff_ms = int((dt.datetime.now(dt.timezone.utc).timestamp() - TRACE_RETENTION_DAYS * 86400) * 1000)
    data["virtual_trace"] = compact_virtual_trace([x for x in trace if x.get("ts_ms", 0) >= cutoff_ms])[-TRACE_MAX_POINTS:]


def build_price_profile(intervals, date_str):
    arr = [it for it in intervals if it["dt"].date().isoformat() == date_str]
    return [[int(it["dt"].timestamp() * 1000), round(it["price_eur"] * 100.0, 3)] for it in arr]


def merge_actual_and_future_profile(actual_points, future_points, date_str, now_ts_ms):
    past = [p for p in actual_points if p.get("date") == date_str and p.get("ts_ms", 0) <= now_ts_ms]
    future = [p for p in future_points if p.get("date") == date_str and p.get("ts_ms", 0) > now_ts_ms]
    merged = sorted(past + future, key=lambda x: x.get("ts_ms", 0))
    return {
        "soc": [[p["ts_ms"], p["soc_pct"]] for p in merged if "soc_pct" in p],
        "power": [[p["ts_ms"], p["power_w"]] for p in merged if "power_w" in p],
        "charge_power": [[p["ts_ms"], p["charge_fc_w"]] for p in merged if "charge_fc_w" in p],
        "discharge_power": [[p["ts_ms"], p["discharge_fc_w"]] for p in merged if "discharge_fc_w" in p],
        "discharge_budget_kwh": [[p["ts_ms"], p.get("discharge_budget_kwh", 0.0)] for p in merged],
    }


def collect_inputs():
    needed = [
        E_PRICE_CURRENT,
        E_PRICE_EUR,
        E_GRID_IMPORT,
        E_GRID_EXPORT,
        E_PV_POWER,
        E_BATTERY_SOC,
        E_BATTERY_MIN_SOC,
        E_BATTERY_POWER,
        E_PV_NEXT_HOUR_ENERGY,
        E_PV_NEXT_HOUR_POWER,
        E_PV_TOMORROW_ENERGY,
        E_WEATHER_CLOUD,
        E_WEATHER_RADIATION,
        E_HEAT_PUMP_POWER,
        E_EV_POWER,
        E_EV_STATUS,
    ]
    s = get_latest_states(needed)

    price_ts, price_vals = build_tibber_price_index(
        local_date_set_between(dt.datetime.now(dt.timezone.utc).timestamp(), dt.datetime.now(dt.timezone.utc).timestamp())
    )
    p_now_eur = tibber_price_eur_at(dt.datetime.now(dt.timezone.utc).timestamp(), price_ts, price_vals)
    p_now = p_now_eur * 100.0 if p_now_eur is not None else None
    if p_now is None:
        p_now = as_float(s[E_PRICE_CURRENT], None)
    if p_now is None:
        p_now_eur = as_float(s[E_PRICE_EUR], None)
        p_now = p_now_eur * 100.0 if p_now_eur is not None else None
    if p_now is None:
        return {"error": "No current price available"}

    pv_raw_kwh = as_float(s[E_PV_NEXT_HOUR_ENERGY], None)
    if pv_raw_kwh is None:
        pv_raw_kwh = max(0.0, as_float(s[E_PV_NEXT_HOUR_POWER], 0.0)) / 1000.0

    cloud = as_float(s[E_WEATHER_CLOUD], None)
    rad = as_float(s[E_WEATHER_RADIATION], None)
    weather = open_meteo_weather()
    if weather:
        cloud = weather["cloud_cover"]
        rad = weather["shortwave_radiation"]

    future_stats = read_tibber_future_price_stats(dt.datetime.now(OPEN_METEO_TZ))
    p_future_max = future_stats["max_ct"] if future_stats else p_now

    wallbox_raw = max(0.0, as_float(s[E_EV_POWER], 0.0))
    wallbox_status = str(s.get(E_EV_STATUS) or "").lower()
    wallbox_w = wallbox_raw if ("charg" in wallbox_status) else 0.0

    return {
        "p_now": p_now,
        "p_future_max": p_future_max,
        "grid_import_w": as_float(s[E_GRID_IMPORT], 0.0),
        "grid_export_w": as_float(s[E_GRID_EXPORT], 0.0),
        "pv_w": as_float(s[E_PV_POWER], 0.0),
        "wallbox_w": wallbox_w,
        "bat_in_out_w": as_float(s[E_BATTERY_POWER], 0.0),
        "soc": as_float(s[E_BATTERY_SOC], None),
        "soc_min_pct": as_float(s[E_BATTERY_MIN_SOC], SOC_MIN),
        "hp_w": as_float(s[E_HEAT_PUMP_POWER], 0.0),
        "pv_raw_kwh": pv_raw_kwh,
        "pv_tomorrow_kwh": as_float(s[E_PV_TOMORROW_ENERGY], None),
        "cloud": 50.0 if cloud is None else cloud,
        "rad": 0.0 if rad is None else rad,
        "weather": weather,
    }


def read_tibber_intervals_for_dates(date_set):
    intervals = read_tibber_intervals_all()
    return [it for it in intervals if it["dt"].date().isoformat() in date_set]


def read_tibber_intervals_all():
    if _RUNTIME_PRICE_INTERVALS:
        merged = {}
        for item in _RUNTIME_PRICE_INTERVALS:
            try:
                value = float(item.get("price_per_kwh", item.get("price", item.get("total"))))
                timestamp = item.get("start_time") or item.get("startsAt") or item.get("start")
                parsed = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=OPEN_METEO_TZ)
                price_eur = value / 100.0 if value >= 2.0 else value
                merged[parsed.timestamp()] = {"ts": parsed.timestamp(), "dt": parsed, "price_eur": price_eur}
            except (AttributeError, TypeError, ValueError):
                continue
        return [merged[key] for key in sorted(merged)]
    return []


def apply_eex_proxy_prices(intervals, eex_days, today_date, tomorrow_date):
    """Fill missing tomorrow Tibber prices with an EEX-anchored retail proxy."""
    existing = list(intervals)
    tomorrow_iso = tomorrow_date.isoformat()
    tomorrow_real = [it for it in existing if it["dt"].date() == tomorrow_date and it.get("source", "tibber") == "tibber"]
    if len(tomorrow_real) >= EEX_PROXY_MIN_FULL_DAY_SLOTS:
        return sorted(existing, key=lambda it: it["dt"]), "tibber"

    proxy = build_eex_proxy_day_prices(existing, eex_days, today_date, tomorrow_date)
    if not proxy:
        return sorted(existing, key=lambda it: it["dt"]), "missing"

    without_incomplete_tomorrow = [it for it in existing if it["dt"].date().isoformat() != tomorrow_iso]
    return sorted(without_incomplete_tomorrow + proxy, key=lambda it: it["dt"]), "eex_proxy"


def build_eex_proxy_day_prices(tibber_intervals, eex_days, reference_date, target_date):
    """Build a 96-slot retail price proxy from EEX base/peak and recent Tibber shape."""
    target_ctx = (eex_days or {}).get(target_date.isoformat(), {})
    base_ct = _eex_settlement_ct(target_ctx, "base")
    peak_ct = _eex_settlement_ct(target_ctx, "peak")
    if base_ct is None:
        return []
    if peak_ct is None:
        peak_ct = base_ct

    by_date = _tibber_intervals_by_date(tibber_intervals)
    recent_days = [
        day
        for day in sorted(by_date.keys())
        if day < target_date and len(by_date.get(day, [])) >= EEX_PROXY_MIN_FULL_DAY_SLOTS
    ][-EEX_PROXY_RECENT_DAYS:]
    slot_offsets = _recent_slot_offsets(by_date, recent_days)
    reference_markup_base, reference_markup_peak = _retail_markups_from_reference_day(
        by_date,
        eex_days,
        reference_date,
    )
    day_avg_ct = base_ct + reference_markup_base
    peak_avg_ct = peak_ct + reference_markup_peak

    raw = []
    for slot in range(SLOTS_PER_DAY):
        raw.append(day_avg_ct + slot_offsets.get(slot, 0.0))

    peak_slots = [slot for slot in range(SLOTS_PER_DAY) if _is_peak_slot(slot)]
    off_slots = [slot for slot in range(SLOTS_PER_DAY) if slot not in peak_slots]
    raw_peak_avg = statistics.mean(raw[slot] for slot in peak_slots)
    raw_off_avg = statistics.mean(raw[slot] for slot in off_slots)
    desired_off_avg = ((day_avg_ct * SLOTS_PER_DAY) - (peak_avg_ct * len(peak_slots))) / len(off_slots)

    out = []
    local_midnight = dt.datetime.combine(target_date, dt.time.min, tzinfo=OPEN_METEO_TZ)
    for slot, price_ct in enumerate(raw):
        if _is_peak_slot(slot):
            price_ct += peak_avg_ct - raw_peak_avg
        else:
            price_ct += desired_off_avg - raw_off_avg
        price_ct = clamp(price_ct, EEX_PROXY_MIN_PRICE_CT, EEX_PROXY_MAX_PRICE_CT)
        slot_dt = local_midnight + dt.timedelta(minutes=15 * slot)
        out.append(
            {
                "dt": slot_dt,
                "ts": slot_dt.isoformat(),
                "price_eur": round(price_ct / 100.0, 5),
                "source": "eex_proxy",
            }
        )
    return out


def _eex_settlement_ct(eex_days, product):
    try:
        value = eex_days.get(product, {}).get("settl_ct_kwh")
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _tibber_intervals_by_date(intervals):
    by_date = {}
    for it in intervals:
        if it.get("source", "tibber") != "tibber":
            continue
        by_date.setdefault(it["dt"].date(), []).append(it)
    for day in list(by_date.keys()):
        by_date[day] = sorted(by_date[day], key=lambda item: item["dt"])
    return by_date


def _recent_slot_offsets(by_date, recent_days):
    slot_values = {slot: [] for slot in range(SLOTS_PER_DAY)}
    for day in recent_days:
        items = by_date.get(day, [])
        prices = [float(it["price_eur"]) * 100.0 for it in items]
        if not prices:
            continue
        day_avg = statistics.mean(prices)
        for it in items:
            slot = slot_index_for_dt(it["dt"])
            slot_values[slot].append(float(it["price_eur"]) * 100.0 - day_avg)
    return {
        slot: statistics.median(values)
        for slot, values in slot_values.items()
        if values
    }


def _retail_markups_from_reference_day(by_date, eex_days, reference_date):
    reference_items = by_date.get(reference_date, [])
    reference_eex = (eex_days or {}).get(reference_date.isoformat(), {})
    base_ct = _eex_settlement_ct(reference_eex, "base")
    peak_ct = _eex_settlement_ct(reference_eex, "peak")
    if len(reference_items) < EEX_PROXY_MIN_FULL_DAY_SLOTS or base_ct is None:
        return 20.0, 20.0
    prices = [float(it["price_eur"]) * 100.0 for it in reference_items]
    peak_prices = [float(it["price_eur"]) * 100.0 for it in reference_items if _is_peak_slot(slot_index_for_dt(it["dt"]))]
    base_markup = statistics.mean(prices) - base_ct
    if peak_prices and peak_ct is not None:
        peak_markup = statistics.mean(peak_prices) - peak_ct
    else:
        peak_markup = base_markup
    return (
        clamp(base_markup, EEX_PROXY_MIN_RETAIL_MARKUP_CT, EEX_PROXY_MAX_BASE_RETAIL_MARKUP_CT),
        clamp(peak_markup, EEX_PROXY_MIN_RETAIL_MARKUP_CT, EEX_PROXY_MAX_PEAK_RETAIL_MARKUP_CT),
    )


def _is_peak_slot(slot):
    hour = int(slot) // 4
    return 8 <= hour < 20


def local_date_set_between(start_ts, end_ts):
    start_day = dt.datetime.fromtimestamp(float(start_ts), dt.timezone.utc).astimezone(OPEN_METEO_TZ).date()
    end_day = dt.datetime.fromtimestamp(float(end_ts), dt.timezone.utc).astimezone(OPEN_METEO_TZ).date()
    days = set()
    cur = start_day
    while cur <= end_day:
        days.add(cur.isoformat())
        cur += dt.timedelta(days=1)
    return days


def build_tibber_price_index(date_set):
    intervals = read_tibber_intervals_for_dates(date_set)
    pairs = sorted((float(it["dt"].timestamp()), float(it["price_eur"])) for it in intervals)
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


def build_production_forecast(
    intervals,
    samples,
    targets,
    *,
    now_local,
    weather_factor,
    forecast_tomorrow_kwh,
    load_bias,
    load_bias_slots,
    pv_bias_slots,
    pv_now_actual_w,
    pv_global_bias,
    capacity_events,
    shadow_history=(),
):
    """Build the sole production forecast from immutable inputs."""
    started = time.perf_counter()
    request = ForecastRequest(
        as_of_ms=int(now_local.timestamp() * 1000),
        timezone=str(getattr(OPEN_METEO_TZ, "key", OPEN_METEO_TZ)),
        slots=tuple(
            SlotKey(
                int(item["dt"].timestamp() * 1000),
                int(item["dt"].timestamp() * 1000) + int(SLOT_H * 3600 * 1000),
            )
            for item in intervals
        ),
    )
    immutable_samples = tuple(
        LegacyForecastSample.from_mapping(sample) for sample in samples[-6000:]
    )
    latest = samples[-1] if samples else {}
    context = LoadForecastContext(
        house_load_no_ev_w=max(0.0, float(latest.get("load_w", 0.0) or 0.0)),
        drivers=(
            LoadDriverSnapshot(
                "heat_pump", max(0.0, float(latest.get("hp_w", 0.0) or 0.0))
            ),
        ),
    )
    config = LegacyForecastConfig(
        timezone=request.timezone,
        load_bias=float(load_bias),
        load_slot_biases=tuple(float(value) for value in load_bias_slots),
        pv_global_bias=float(pv_global_bias),
        pv_slot_biases=tuple(float(value) for value in pv_bias_slots),
        current_weather_factor=float(weather_factor),
        current_pv_w=(
            None if pv_now_actual_w is None else max(0.0, float(pv_now_actual_w))
        ),
        tomorrow_date=(
            intervals[0]["dt"].date() + dt.timedelta(days=1)
        ).isoformat(),
        tomorrow_energy_kwh=(
            None if forecast_tomorrow_kwh is None else float(forecast_tomorrow_kwh)
        ),
        capacity_events=tuple(
            (str(item[0]), float(item[1]), float(item[2]))
            for item in capacity_events
        ),
    )
    forecast = build_legacy_forecast(
        request, immutable_samples, tuple(targets), context, config
    )
    load_w = [slot.energy.p50_kwh / SLOT_H * 1000.0 for slot in forecast.load.slots]
    pv_w = [slot.energy.p50_kwh / SLOT_H * 1000.0 for slot in forecast.pv.slots]
    if len(load_w) != len(intervals) or len(pv_w) != len(intervals):
        raise ValueError("production forecast does not match requested slot grid")
    diagnostics = {
        "source": "extracted",
        "slot_count": len(request.slots),
        "runtime_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "model_version": "legacy-extracted-v1",
    }
    if shadow_history:
        try:
            diagnostics["shadow_comparison"] = evaluate_feature_store_shadow(
                production=forecast,
                request=request,
                history=tuple(shadow_history),
                targets=tuple(targets),
                context=context,
                config=config,
            )
        except Exception as err:  # noqa: BLE001 - shadow must never affect control.
            diagnostics["shadow_comparison"] = {
                "generated_at_ms": request.as_of_ms,
                "status": "error",
                "reason": f"{type(err).__name__}: {err}",
                "authoritative": False,
            }
    return load_w, pv_w, diagnostics


def _future_higher_value_load_reserve_kwh(
    slots,
    points,
    current_idx,
    *,
    pv_recovery_confidence=PV_RECOVERY_CONFIDENCE,
):
    """Return later high-value load not covered by planned recharge.

    A future charge is replacement energy, not a binary reset of inventory
    scarcity. Grid charge is treated as firm; forecast PV charge receives the
    same confidence discount as the existing PV-recovery budget.
    """
    current_price_ct = float(slots[current_idx]["price_ct"])
    replacement_ac_kwh = 0.0
    reserved_kwh = 0.0
    for future_idx in range(current_idx + 1, len(slots)):
        charge_kwh = max(
            0.0,
            float(points[future_idx].get("charge_fc_w", 0.0)) / 1000.0 * SLOT_H,
        )
        surplus_kwh = max(0.0, float(slots[future_idx].get("surplus_kwh", 0.0)))
        pv_charge_kwh = min(charge_kwh, surplus_kwh)
        grid_charge_kwh = max(0.0, charge_kwh - pv_charge_kwh)
        replacement_ac_kwh += (
            grid_charge_kwh + pv_charge_kwh * pv_recovery_confidence
        ) * ETA_RT

        if float(slots[future_idx]["price_ct"]) <= current_price_ct + 1e-9:
            continue
        future_load_kwh = min(
            MAX_DISCHARGE_E_SLOT_KWH,
            float(
                slots[future_idx].get(
                    "discharge_eligible_kwh",
                    slots[future_idx]["net_pos_kwh"],
                )
            ),
        )
        replacement_used_kwh = min(replacement_ac_kwh, future_load_kwh)
        replacement_ac_kwh -= replacement_used_kwh
        reserved_kwh += future_load_kwh - replacement_used_kwh
    return reserved_kwh


def _pv_spill_recovery_budget_ac_kwh(
    future_surplus_kwh,
    baseline_energy_kwh,
    *,
    confidence=PV_RECOVERY_CONFIDENCE,
    reserve_kwh=PV_RECOVERY_RESERVE_KWH,
):
    """Return discharge energy backed by otherwise uncapturable PV.

    Forecast export is not evidence of missing physical headroom: it can be a
    plan-discretization artifact or an economic choice. Recovery is therefore
    authorized only when confidence-weighted PV surplus exceeds the actual
    storage headroom plus the configured uncertainty reserve.
    """
    recoverable_storage_kwh = max(0.0, float(future_surplus_kwh)) * ETA_C * max(
        0.0, float(confidence)
    )
    baseline_energy_kwh = clamp(
        float(baseline_energy_kwh), MIN_E_KWH, MAX_E_KWH
    )
    headroom_kwh = max(0.0, MAX_E_KWH - baseline_energy_kwh)
    threatened_storage_kwh = max(
        0.0,
        recoverable_storage_kwh - headroom_kwh - max(0.0, float(reserve_kwh)),
    )
    return threatened_storage_kwh * ETA_D


def build_virtual_plan(intervals, samples, start_energy_kwh, weather_factor, forecast_tomorrow_kwh, load_bias, load_bias_slots, pv_bias_slots, initial_mode=0, weather_hourly=None, pv_now_actual_w=None, now_local=None, pv_global_bias=1.0, eex_days=None, inventory_discharge_floor_ct=None, shadow_history=()):
    if not intervals:
        return {"points": [], "today": {}, "tomorrow": {}, "end_soc": 50.0, "price_stats": {}, "daily_costs": {}}

    prices_ct = [x["price_eur"] * 100.0 for x in intervals]
    p_sorted = sorted(prices_ct)
    lo_idx = int(0.3 * (len(p_sorted) - 1))
    hi_idx = int(0.7 * (len(p_sorted) - 1))
    p_low = p_sorted[lo_idx]
    p_high = p_sorted[hi_idx]

    today = intervals[0]["dt"].date().isoformat()
    tomorrow = (intervals[0]["dt"].date() + dt.timedelta(days=1)).isoformat()

    now_local = now_local or dt.datetime.now(OPEN_METEO_TZ)
    forecast_targets = []
    for it in intervals:
        hour_key = it["dt"].replace(minute=0, second=0, microsecond=0).isoformat()
        slot_weather_factor_raw = float((weather_hourly or {}).get(hour_key, {}).get("weather_factor", weather_factor))
        forecast_targets.append(
            LegacyForecastTarget(it["dt"], slot_weather_factor_raw)
        )

    forecast_load_w, forecast_pv_w, forecast_diagnostics = build_production_forecast(
        intervals,
        samples,
        forecast_targets,
        now_local=now_local,
        weather_factor=weather_factor,
        forecast_tomorrow_kwh=forecast_tomorrow_kwh,
        load_bias=load_bias,
        load_bias_slots=load_bias_slots,
        pv_bias_slots=pv_bias_slots,
        pv_now_actual_w=pv_now_actual_w,
        pv_global_bias=pv_global_bias,
        capacity_events=PV_CAPACITY_EVENTS,
        shadow_history=shadow_history,
    )
    forecast_pre = [
        (it, load_w, pv_w, target.weather_factor)
        for it, load_w, pv_w, target in zip(
            intervals,
            forecast_load_w,
            forecast_pv_w,
            forecast_targets,
            strict=True,
        )
    ]

    slots = []
    daily = {}
    for it, load_w_slot, pv_w_slot, _slot_weather_factor_raw in forecast_pre:
        d = it["dt"].date().isoformat()
        load_kwh = max(0.0, load_w_slot / 1000.0 * SLOT_H)
        pv_kwh = max(0.0, pv_w_slot / 1000.0 * SLOT_H)
        net_pos_kwh = max(0.0, load_kwh - pv_kwh)
        surplus_kwh = max(0.0, pv_kwh - load_kwh)
        price_eur = it["price_eur"]
        slots.append(
            {
                "dt": it["dt"],
                "date": d,
                "price_eur": price_eur,
                "price_ct": price_eur * 100.0,
                "weekday_rank": compute_weekday_price_rank(samples, it["dt"].date(), price_eur * 100.0),
                "load_kwh": load_kwh,
                "pv_kwh": pv_kwh,
                "net_pos_kwh": net_pos_kwh,
                "surplus_kwh": surplus_kwh,
            }
        )
        daily.setdefault(d, {"base": 0.0, "with_bat": 0.0})
        daily[d]["base"] += net_pos_kwh * price_eur

    tomorrow_date = intervals[0]["dt"].date() + dt.timedelta(days=1)
    tomorrow_prices = [float(s["price_ct"]) for s in slots if s["date"] == tomorrow]
    tomorrow_min_ct = min(tomorrow_prices) if tomorrow_prices else None
    tomorrow_min_rank = compute_weekday_price_rank(samples, tomorrow_date, tomorrow_min_ct)
    terminal_value_ct = 0.0
    discharge_floor_ct = None
    cheap_window_end_dt = None
    cheap_anchor_ct = None
    cheap_anchor_rank = None
    if tomorrow_min_ct is not None and tomorrow_min_rank is not None and tomorrow_min_rank <= TERMINAL_RANK_THRESHOLD:
        cheapness = clamp((TERMINAL_RANK_THRESHOLD - tomorrow_min_rank) / TERMINAL_RANK_THRESHOLD, 0.0, 1.0)
        spread_ct = max(0.0, p_high - tomorrow_min_ct)
        terminal_value_ct = clamp(spread_ct * (0.5 + cheapness), 0.0, TERMINAL_VALUE_CAP_CT)
        discharge_floor_ct = (tomorrow_min_ct / ETA_RT) + MIN_MARGIN_CT
        cheap_slots = [s for s in slots if s["date"] == tomorrow and s["price_ct"] <= (tomorrow_min_ct + 0.25)]
        if cheap_slots:
            cheap_window_end_dt = max(s["dt"] for s in cheap_slots)
        cheap_anchor_ct = tomorrow_min_ct
        cheap_anchor_rank = tomorrow_min_rank

    horizon_min_slot = min(slots, key=lambda s: float(s["price_ct"])) if slots else None
    if horizon_min_slot is not None:
        horizon_min_ct = float(horizon_min_slot["price_ct"])
        horizon_min_rank = horizon_min_slot.get("weekday_rank")
        if horizon_min_rank is not None:
            cheapness = clamp((0.5 - horizon_min_rank) / 0.5, 0.0, 1.0)
            if cheapness > 0.0:
                tail_date = intervals[-1]["dt"].date() + dt.timedelta(days=1)
                hist_tail_q80 = compute_weekday_price_quantile(samples, tail_date, 0.8)
                tail_ref_ct = hist_tail_q80 or p_high
                eex_days = eex_days or {}
                tail_ctx = eex_days.get(tail_date.isoformat(), {}) if eex_days else {}
                tail_eex_candidates = [
                    tail_ctx.get("base", {}).get("settl_ct_kwh"),
                    tail_ctx.get("peak", {}).get("settl_ct_kwh"),
                ]
                tail_eex_candidates = [float(v) for v in tail_eex_candidates if v is not None]
                if tail_eex_candidates:
                    tail_ref_ct = max([tail_ref_ct] + tail_eex_candidates)
                inferred_tail_value_ct = clamp((max(p_high, tail_ref_ct) - horizon_min_ct) * cheapness, 0.0, TERMINAL_VALUE_CAP_CT)
                if inferred_tail_value_ct > terminal_value_ct:
                    terminal_value_ct = inferred_tail_value_ct
                inferred_floor_ct = (horizon_min_ct / ETA_RT) + MIN_MARGIN_CT
                if discharge_floor_ct is None or inferred_floor_ct < discharge_floor_ct:
                    discharge_floor_ct = inferred_floor_ct
                cheap_slots = [s for s in slots if s["price_ct"] <= (horizon_min_ct + 0.4)]
                if cheap_slots:
                    inferred_cheap_window_end = max(s["dt"] for s in cheap_slots)
                    if cheap_window_end_dt is None or inferred_cheap_window_end > cheap_window_end_dt:
                        cheap_window_end_dt = inferred_cheap_window_end
                if cheap_anchor_ct is None or horizon_min_ct < cheap_anchor_ct:
                    cheap_anchor_ct = horizon_min_ct
                    cheap_anchor_rank = horizon_min_rank
    if inventory_discharge_floor_ct is not None:
        # Existing inventory has its own cost basis. A future cheap recharge floor
        # must not block discharging already cheap stored energy in higher-price slots.
        discharge_floor_ct = min(float(inventory_discharge_floor_ct), float(discharge_floor_ct or inventory_discharge_floor_ct))

    for sl in slots:
        # The automatic strategy only values serving forecast household load.
        # Battery discharge must not create forecast export unless explicit
        # battery export economics are added later.
        sl["discharge_eligible_kwh"] = (
            min(float(sl["net_pos_kwh"]), MAX_DISCHARGE_E_SLOT_KWH)
            if DISCHARGE_ENABLED
            else 0.0
        )

    # Cost-optimal plan over 48h via dynamic programming on discretized SoC states.
    e_step = ENERGY_STEP_KWH
    n_states = int(round((MAX_E_KWH - MIN_E_KWH) / e_step)) + 1
    energies = [MIN_E_KWH + i * e_step for i in range(n_states)]

    def idx_from_energy(e):
        i = int(round((clamp(e, MIN_E_KWH, MAX_E_KWH) - MIN_E_KWH) / e_step))
        return max(0, min(n_states - 1, i))

    start_idx = idx_from_energy(start_energy_kwh)
    n_slots = len(slots)
    inf = 10**18
    # mode idx: 0=discharge, 1=idle, 2=charge
    mode_values = [-1, 0, 1]
    mode_to_idx = {-1: 0, 0: 1, 1: 2}
    dp = [[[inf] * 3 for _ in range(n_states)] for _ in range(n_slots + 1)]
    prev = [[[None] * 3 for _ in range(n_states)] for _ in range(n_slots + 1)]
    dp[0][start_idx][mode_to_idx.get(initial_mode, mode_to_idx[0])] = 0.0

    max_charge_delta_e = ETA_C * MAX_CHARGE_E_SLOT_KWH
    max_discharge_delta_e = MAX_DISCHARGE_E_SLOT_KWH / ETA_D
    future_peak_price_ct = [0.0] * (n_slots + 1)
    for t in range(n_slots - 1, -1, -1):
        future_peak_price_ct[t] = max(future_peak_price_ct[t + 1], float(slots[t]["price_ct"]))
    future_pv_surplus_kwh = [0.0] * (n_slots + 1)
    for t in range(n_slots - 1, -1, -1):
        future_pv_surplus_kwh[t] = future_pv_surplus_kwh[t + 1] + float(slots[t]["surplus_kwh"])

    def transition_candidate(t, e_now, prev_mode, e_next):
        sl = slots[t]
        price = sl["price_eur"]
        price_ct = sl["price_ct"]
        net_pos = sl["net_pos_kwh"]
        surplus = sl["surplus_kwh"]
        discharge_eligible = sl.get("discharge_eligible_kwh", net_pos)
        delta = e_next - e_now
        charge_in = 0.0
        discharge_out = 0.0
        if delta > 1e-9:
            charge_in = delta / ETA_C
            if charge_in > MAX_CHARGE_E_SLOT_KWH + 1e-9:
                return None
            if not PV_CHARGING_ENABLED and not GRID_CHARGING_ENABLED:
                return None
            if not GRID_CHARGING_ENABLED and charge_in > surplus + 1e-9:
                return None
            if not PV_CHARGING_ENABLED and surplus > 1e-9:
                return None
        elif delta < -1e-9:
            if not DISCHARGE_ENABLED:
                return None
            discharge_out = (-delta) * ETA_D
            if discharge_out > discharge_eligible + 1e-9:
                return None
            if discharge_out > MAX_DISCHARGE_E_SLOT_KWH + 1e-9:
                return None
            if discharge_floor_ct is not None and price_ct < discharge_floor_ct:
                return None

        mode_now = 0
        if charge_in > 1e-4:
            mode_now = 1
        elif discharge_out > 1e-4:
            mode_now = -1

        # Plan modes are economic intent, not actuator state: live PV surplus
        # may turn a planned discharge slot into charge-follow. Artificial mode
        # transition costs would therefore distort slot value. RTE, margin and
        # micro-cycle suppression remain the economic cycle guards.
        switch_cost = 0.0

        charge_from_grid = max(0.0, charge_in - surplus)
        if charge_from_grid > 1e-9:
            future_value_ct = future_peak_price_ct[t + 1] * ETA_RT
            if future_value_ct < (price_ct + MIN_MARGIN_CT):
                return None
            later_cheaper_charge_capacity_kwh = sum(
                MAX_CHARGE_E_SLOT_KWH
                for future_sl in slots[t + 1 :]
                if float(future_sl["price_ct"]) + CHARGE_DEFERRAL_MARGIN_CT < price_ct
            )
            if later_cheaper_charge_capacity_kwh > 1e-9:
                profitable_discharge_need_ac_kwh = sum(
                    min(MAX_DISCHARGE_E_SLOT_KWH, float(future_sl["net_pos_kwh"]))
                    for future_sl in slots[t + 1 :]
                    if float(future_sl["price_ct"]) >= ((price_ct / ETA_RT) + MIN_MARGIN_CT)
                )
                current_usable_ac_kwh = max(0.0, (e_now - MIN_E_KWH) * ETA_D)
                additional_profitable_charge_in_kwh = max(
                    0.0, (profitable_discharge_need_ac_kwh - current_usable_ac_kwh) / ETA_RT
                )
                if later_cheaper_charge_capacity_kwh >= (additional_profitable_charge_in_kwh - 1e-6):
                    return None
        grid_import = max(0.0, net_pos - min(discharge_out, net_pos)) + charge_from_grid
        cheap_charge_credit = 0.0
        weekday_rank = sl.get("weekday_rank")
        if charge_from_grid > 1e-9 and weekday_rank is not None and weekday_rank <= CHEAP_CHARGE_RANK_THRESHOLD:
            cheapness = clamp((CHEAP_CHARGE_RANK_THRESHOLD - weekday_rank) / CHEAP_CHARGE_RANK_THRESHOLD, 0.0, 1.0)
            bonus_ct = clamp((p_high - price_ct) * (0.5 + cheapness), 0.0, CHEAP_CHARGE_BONUS_CAP_CT)
            cheap_charge_credit = charge_from_grid * (bonus_ct / 100.0)
        step_cost = (grid_import * price) + switch_cost - cheap_charge_credit
        return step_cost, charge_in, discharge_out, grid_import, mode_now

    for t in range(n_slots):
        sl = slots[t]
        net_pos = sl["net_pos_kwh"]
        discharge_eligible = sl.get("discharge_eligible_kwh", net_pos)
        for i, e_now in enumerate(energies):
            min_e_next = max(MIN_E_KWH, e_now - min(max_discharge_delta_e, discharge_eligible / ETA_D))
            max_e_next = min(MAX_E_KWH, e_now + max_charge_delta_e)
            i_min = idx_from_energy(min_e_next)
            i_max = idx_from_energy(max_e_next)
            for prev_mode_idx, prev_mode in enumerate(mode_values):
                base_cost = dp[t][i][prev_mode_idx]
                if base_cost >= inf:
                    continue
                for j in range(i_min, i_max + 1):
                    transition = transition_candidate(t, e_now, prev_mode, energies[j])
                    if transition is None:
                        continue
                    step_cost, charge_in, discharge_out, grid_import, mode_now = transition
                    mode_now_idx = mode_to_idx[mode_now]
                    cand = base_cost + step_cost
                    if cand < dp[t + 1][j][mode_now_idx]:
                        dp[t + 1][j][mode_now_idx] = cand
                        prev[t + 1][j][mode_now_idx] = (i, prev_mode_idx, charge_in, discharge_out, grid_import, mode_now)

    def terminal_adjusted_cost(i, mi):
        end_energy_above_min = max(0.0, energies[i] - MIN_E_KWH)
        terminal_credit = (terminal_value_ct / 100.0) * end_energy_above_min
        return dp[n_slots][i][mi] - terminal_credit

    end_idx, end_mode_idx = min(
        ((i, mi) for i in range(n_states) for mi in range(3)),
        key=lambda x: terminal_adjusted_cost(x[0], x[1]),
    )

    # Reconstruct optimized trajectory.
    points = [None] * n_slots
    path_before_idx = [start_idx] * n_slots
    path_after_idx = [start_idx] * n_slots
    path_discharge_out = [0.0] * n_slots
    path_charge_in = [0.0] * n_slots
    cur = end_idx
    cur_mode_idx = end_mode_idx
    for t in range(n_slots, 0, -1):
        rec = prev[t][cur][cur_mode_idx]
        if rec is None:
            rec = (cur, mode_to_idx[0], 0.0, 0.0, slots[t - 1]["net_pos_kwh"], 0)
        prev_idx, prev_mode_idx, charge_in, discharge_out, grid_import, mode_now = rec
        sl = slots[t - 1]
        idx = t - 1
        transition = transition_candidate(idx, energies[prev_idx], mode_values[prev_mode_idx], energies[cur])
        path_before_idx[idx] = prev_idx
        path_after_idx[idx] = cur
        path_discharge_out[idx] = max(0.0, discharge_out)
        path_charge_in[idx] = max(0.0, charge_in)
        grid_export = max(0.0, sl["surplus_kwh"] - charge_in) + max(0.0, discharge_out - sl["net_pos_kwh"])
        p_bat_w = ((charge_in - discharge_out) / SLOT_H) * 1000.0
        mode = "idle"
        if mode_now > 0:
            mode = "charge"
        elif mode_now < 0:
            mode = "discharge"
        daily[sl["date"]]["with_bat"] += grid_import * sl["price_eur"]
        points[t - 1] = {
            "ts_ms": int(sl["dt"].timestamp() * 1000),
            "date": sl["date"],
            "price_ct": round(sl["price_ct"], 3),
            "soc_pct": round((energies[prev_idx] / CAP_KWH) * 100.0, 2),
            "power_w": round(p_bat_w, 1),
            "charge_fc_w": round(max(0.0, p_bat_w), 1),
            "discharge_fc_w": round(max(0.0, -p_bat_w), 1),
            "mode": mode,
            "load_fc_w": round((sl["load_kwh"] / SLOT_H) * 1000.0, 1),
            "pv_fc_w": round((sl["pv_kwh"] / SLOT_H) * 1000.0, 1),
            "discharge_eligible_fc_w": round((sl.get("discharge_eligible_kwh", sl["net_pos_kwh"]) / SLOT_H) * 1000.0, 1),
            "grid_import_fc_w": round((grid_import / SLOT_H) * 1000.0, 1),
            "grid_export_fc_w": round((grid_export / SLOT_H) * 1000.0, 1),
            "grid_net_fc_w": round(((grid_import - grid_export) / SLOT_H) * 1000.0, 1),
        }
        cur = prev_idx
        cur_mode_idx = prev_mode_idx

    points = suppress_uneconomic_micro_cycles(points, start_energy_kwh)

    recovery_lookahead_slots = max(1, int(round(PV_RECOVERY_LOOKAHEAD_H / SLOT_H)))
    def recovery_window_end_idx(t):
        price_ct = float(slots[t]["price_ct"])
        default_end = min(n_slots, t + 1 + recovery_lookahead_slots)
        for idx in range(t + 1, default_end):
            if float(slots[idx]["price_ct"]) > price_ct + SCARCE_VALUE_TIE_CT:
                return idx
        return default_end

    def safe_pv_recovery_ac_kwh(t, baseline_after_e):
        if not PV_CHARGING_ENABLED:
            return 0.0
        end_idx = recovery_window_end_idx(t)
        future_surplus_kwh = max(0.0, future_pv_surplus_kwh[t + 1] - future_pv_surplus_kwh[end_idx])
        return _pv_spill_recovery_budget_ac_kwh(
            future_surplus_kwh,
            baseline_after_e,
        )

    def pre_slot_energy_kwh(t):
        return clamp(
            (float(points[t].get("soc_pct", 0.0)) / 100.0) * CAP_KWH,
            MIN_E_KWH,
            MAX_E_KWH,
        )

    def point_charge_kwh(t):
        return max(0.0, float(points[t].get("charge_fc_w", 0.0)) / 1000.0 * SLOT_H)

    def point_discharge_kwh(t):
        return max(0.0, float(points[t].get("discharge_fc_w", 0.0)) / 1000.0 * SLOT_H)

    def post_slot_energy_kwh(t):
        return clamp(
            pre_slot_energy_kwh(t)
            + point_charge_kwh(t) * ETA_C
            - point_discharge_kwh(t) / ETA_D,
            MIN_E_KWH,
            MAX_E_KWH,
        )

    def point_grid_charge_kwh(t):
        return max(0.0, point_charge_kwh(t) - float(slots[t]["surplus_kwh"]))

    def future_higher_value_load_kwh(t):
        """Return high-value forecast load not replaced by planned recharge."""
        return _future_higher_value_load_reserve_kwh(slots, points, t)

    def explicit_discharge_budget_kwh(t):
        # A budget may coexist with free PV charging, but never with paid
        # charging. Otherwise live discharge lowers SoC and the next optimizer
        # run immediately buys the same energy back through must-charge.
        if point_grid_charge_kwh(t) > 1e-6:
            return 0.0

        slot_energy_kwh = pre_slot_energy_kwh(t)
        available_ac_kwh = max(0.0, (slot_energy_kwh - MIN_E_KWH) * ETA_D)
        max_total_discharge_kwh = min(MAX_DISCHARGE_E_SLOT_KWH, available_ac_kwh)
        if max_total_discharge_kwh <= 1e-6:
            return 0.0

        sl = slots[t]
        price_ct = float(sl["price_ct"])
        if price_ct < PV_EXPORT_OPPORTUNITY_CT + MIN_MARGIN_CT:
            return 0.0

        safe_recovery_kwh = safe_pv_recovery_ac_kwh(t, post_slot_energy_kwh(t))
        # If live conditions turn a forecast PV-charge slot into discharge, the
        # later free PV must first replace the missed planned charge. Only the
        # remaining recovery energy is safe to expose as a discharge budget.
        missed_plan_recovery_kwh = point_charge_kwh(t) * ETA_RT
        pv_recovery_budget_kwh = min(
            max_total_discharge_kwh,
            max(0.0, safe_recovery_kwh - missed_plan_recovery_kwh),
        )

        scarce_budget_kwh = 0.0
        scarce_floor_ct = float(discharge_floor_ct or 0.0)
        if point_charge_kwh(t) <= 1e-6 and price_ct >= scarce_floor_ct:
            higher_value_need_kwh = future_higher_value_load_kwh(t)
            scarce_budget_kwh = max(
                0.0,
                available_ac_kwh + safe_recovery_kwh - higher_value_need_kwh,
            )

        # Expected dispatch is a subset of commercial permission. The live
        # controller may use a larger budget for unexpected household load, but
        # it must never be unable to execute an optimizer-planned discharge.
        planned_discharge_kwh = point_discharge_kwh(t)
        budget_kwh = max(
            planned_discharge_kwh,
            pv_recovery_budget_kwh,
            min(max_total_discharge_kwh, scarce_budget_kwh),
        )

        return round(min(max_total_discharge_kwh, budget_kwh), 3)

    for idx, point in enumerate(points):
        point["discharge_budget_kwh"] = explicit_discharge_budget_kwh(idx)

    daily_with_bat = {}
    for p in points:
        d = p.get("date")
        daily_with_bat.setdefault(d, 0.0)
        grid_import_kwh = max(0.0, float(p.get("grid_import_fc_w", 0.0)) / 1000.0 * SLOT_H)
        daily_with_bat[d] += grid_import_kwh * (float(p.get("price_ct", 0.0)) / 100.0)

    daily_costs = {}
    for d, vals in daily.items():
        daily_costs[d] = {
            "base_eur": round(vals["base"], 3),
            "with_bat_eur": round(daily_with_bat.get(d, vals["with_bat"]), 3),
            "saving_eur": round(vals["base"] - daily_with_bat.get(d, vals["with_bat"]), 3),
        }

    return {
        "points": points,
        "today": {
            "date": today,
            "saving_eur": daily_costs.get(today, {}).get("saving_eur", 0.0),
        },
        "tomorrow": {
            "date": tomorrow,
            "saving_eur": daily_costs.get(tomorrow, {}).get("saving_eur", 0.0),
        },
        "end_soc": round((energies[end_idx] / CAP_KWH) * 100.0, 2),
        "price_stats": {
            "p_low": round(p_low, 2),
            "p_high": round(p_high, 2),
            "avg": round(sum(prices_ct) / len(prices_ct), 2),
            "min": round(min(prices_ct), 2),
            "max": round(max(prices_ct), 2),
            "tomorrow_min_rank": round(tomorrow_min_rank, 3) if tomorrow_min_rank is not None else None,
            "terminal_value_ct": round(terminal_value_ct, 3),
            "discharge_floor_ct": round(discharge_floor_ct, 3) if discharge_floor_ct is not None else None,
            "cheap_anchor_ct": round(cheap_anchor_ct, 3) if cheap_anchor_ct is not None else None,
            "cheap_anchor_rank": round(cheap_anchor_rank, 3) if cheap_anchor_rank is not None else None,
        },
        "daily_costs": daily_costs,
        "forecast_diagnostics": forecast_diagnostics,
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
        b["discharge_budget_kwh"] = max(float(b.get("discharge_budget_kwh", 0.0)), float(p.get("discharge_budget_kwh", 0.0)))
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
                "discharge_budget_kwh": round(float(b.get("discharge_budget_kwh", 0.0)), 3),
                "pv_fc_w": round(b["pv_fc_w"] / n, 1),
                "grid_import_fc_w": round(b["grid_import_fc_w"] / n, 1),
                "grid_export_fc_w": round(b["grid_export_fc_w"] / n, 1),
                "grid_net_fc_w": round(b["grid_net_fc_w"] / n, 1),
            }
        )
    return out


def suppress_uneconomic_micro_cycles(points, start_energy_kwh):
    if not points:
        return points

    for idx, p in enumerate(points):
        if float(p.get("discharge_fc_w", 0.0)) <= 1.0:
            continue
        recent_charge_cost_ct_kwh = 0.0
        recent_charge_kwh = 0.0
        for j in range(max(0, idx - MICROCYCLE_LOOKBACK_SLOTS), idx):
            charge_kwh = max(0.0, float(points[j].get("charge_fc_w", 0.0)) / 1000.0 * SLOT_H)
            if charge_kwh <= 1e-6:
                continue
            load_kwh = max(0.0, float(points[j].get("load_fc_w", 0.0)) / 1000.0 * SLOT_H)
            pv_kwh = max(0.0, float(points[j].get("pv_fc_w", 0.0)) / 1000.0 * SLOT_H)
            surplus_kwh = max(0.0, pv_kwh - load_kwh)
            grid_charge_kwh = max(0.0, charge_kwh - surplus_kwh)
            if grid_charge_kwh <= 1e-6:
                continue
            recent_charge_cost_ct_kwh += grid_charge_kwh * float(points[j].get("price_ct", 0.0))
            recent_charge_kwh += grid_charge_kwh
        if recent_charge_kwh <= 1e-6:
            continue
        recent_charge_avg_ct = recent_charge_cost_ct_kwh / recent_charge_kwh
        min_viable_discharge_ct = (recent_charge_avg_ct / ETA_RT) + MIN_MARGIN_CT
        if float(p.get("price_ct", 0.0)) <= min_viable_discharge_ct:
            p["power_w"] = 0.0
            p["charge_fc_w"] = 0.0
            p["discharge_fc_w"] = 0.0
            p["mode"] = "idle"

    energy = clamp(float(start_energy_kwh), MIN_E_KWH, MAX_E_KWH)
    for p in points:
        load_kwh = max(0.0, float(p.get("load_fc_w", 0.0)) / 1000.0 * SLOT_H)
        pv_kwh = max(0.0, float(p.get("pv_fc_w", 0.0)) / 1000.0 * SLOT_H)
        net_pos_kwh = max(0.0, load_kwh - pv_kwh)
        surplus_kwh = max(0.0, pv_kwh - load_kwh)
        discharge_eligible_kwh = net_pos_kwh

        req_charge_in = max(0.0, float(p.get("charge_fc_w", 0.0)) / 1000.0 * SLOT_H)
        req_discharge_out = max(0.0, float(p.get("discharge_fc_w", 0.0)) / 1000.0 * SLOT_H)

        max_charge_in = max(0.0, (MAX_E_KWH - energy) / ETA_C)
        charge_in = min(req_charge_in, MAX_CHARGE_E_SLOT_KWH, max_charge_in)
        if not GRID_CHARGING_ENABLED:
            charge_in = min(charge_in, surplus_kwh if PV_CHARGING_ENABLED else 0.0)
        elif not PV_CHARGING_ENABLED and charge_in <= surplus_kwh:
            charge_in = 0.0

        max_discharge_out = max(0.0, (energy - MIN_E_KWH) * ETA_D)
        discharge_out = min(req_discharge_out, MAX_DISCHARGE_E_SLOT_KWH, discharge_eligible_kwh, max_discharge_out)
        if not DISCHARGE_ENABLED:
            discharge_out = 0.0

        p["soc_pct"] = round((energy / CAP_KWH) * 100.0, 2)
        p_bat_w = ((charge_in - discharge_out) / SLOT_H) * 1000.0
        grid_import = max(0.0, net_pos_kwh - min(discharge_out, net_pos_kwh)) + max(0.0, charge_in - surplus_kwh)
        grid_export = max(0.0, surplus_kwh - charge_in) + max(0.0, discharge_out - net_pos_kwh)

        p["power_w"] = round(p_bat_w, 1)
        p["charge_fc_w"] = round(max(0.0, p_bat_w), 1)
        p["discharge_fc_w"] = round(max(0.0, -p_bat_w), 1)
        p["mode"] = "charge" if p_bat_w > 1e-3 else ("discharge" if p_bat_w < -1e-3 else "idle")
        p["grid_import_fc_w"] = round((grid_import / SLOT_H) * 1000.0, 1)
        p["grid_export_fc_w"] = round((grid_export / SLOT_H) * 1000.0, 1)
        p["grid_net_fc_w"] = round(((grid_import - grid_export) / SLOT_H) * 1000.0, 1)

        energy = clamp(energy + charge_in * ETA_C - (discharge_out / ETA_D), MIN_E_KWH, MAX_E_KWH)

    return points


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


def classify_discharge_mode(future_points, current_price_ct, usable_energy_ac_kwh):
    if not future_points:
        return {
            "mode": "idle",
            "next_charge_window_start": None,
            "remaining_discharge_budget_kwh": 0.0,
            "expected_net_load_until_charge_kwh": 0.0,
            "required_discharge_power_w": 0.0,
            "slot_value_score": 0.0,
            "coverage_ratio": 0.0,
            "current_allocated_power_w": 0.0,
            "valuable_load_kwh": 0.0,
            "current_is_local_peak": False,
        }

    current = future_points[0]
    charge_idx = next((i for i, p in enumerate(future_points[1:], start=1) if p.get("mode") == "charge"), None)
    horizon = future_points if charge_idx is None else future_points[:charge_idx]
    if not horizon:
        horizon = [current]

    price_window = [float(p.get("price_ct", current_price_ct or 0.0)) for p in horizon]
    p_min = min(price_window) if price_window else (current_price_ct or 0.0)
    p_max = max(price_window) if price_window else (current_price_ct or 0.0)
    current_is_local_peak = bool(price_window) and abs(float(current.get("price_ct", current_price_ct or 0.0)) - p_max) < 0.05
    if p_max - p_min < 0.1:
        slot_value_score = 0.0
    else:
        slot_value_score = clamp(((current_price_ct or p_min) - p_min) / (p_max - p_min), 0.0, 1.0)

    next_charge_window_start = None
    if charge_idx is not None:
        next_charge_window_start = future_points[charge_idx].get("ts_ms")

    slots = []
    expected_net_load_until_charge_kwh = 0.0
    for idx, p in enumerate(horizon):
        absorbable_kwh = min(
            MAX_DISCHARGE_E_SLOT_KWH,
            max(0.0, float(p.get("grid_import_fc_w", 0.0)) + float(p.get("discharge_fc_w", 0.0))) / 1000.0 * SLOT_H,
        )
        expected_net_load_until_charge_kwh += absorbable_kwh
        slots.append(
            {
                "idx": idx,
                "price_ct": float(p.get("price_ct", current_price_ct or 0.0)),
                "absorbable_kwh": absorbable_kwh,
                "allocated_kwh": 0.0,
            }
        )

    remaining = max(0.0, float(usable_energy_ac_kwh))
    current_price_ref = max(0.0, float(current_price_ct or p_max or 0.0))
    if remaining > 1e-6:
        switch_energy_kwh = (SWITCH_PENALTY_MIN / 60.0) * (SWITCH_PENALTY_REF_W / 1000.0)
        deferral_penalty_ct = (switch_energy_kwh * current_price_ref) / remaining
    else:
        deferral_penalty_ct = 0.0

    def slot_priority(slot):
        near_peak = (p_max - slot["price_ct"]) <= deferral_penalty_ct
        return (-(p_max if near_peak else slot["price_ct"]), slot["idx"])

    for slot in sorted(slots, key=slot_priority):
        alloc = min(slot["absorbable_kwh"], remaining)
        slot["allocated_kwh"] = alloc
        remaining -= alloc
        if remaining <= 1e-6:
            break

    current_slot = next((s for s in slots if s["idx"] == 0), {"allocated_kwh": 0.0, "absorbable_kwh": 0.0})
    current_allocated_power_w = (current_slot["allocated_kwh"] / SLOT_H) * 1000.0
    valuable_load_kwh = sum(s["absorbable_kwh"] for s in slots if s["allocated_kwh"] > 1e-6)
    allocated_total_kwh = sum(s["allocated_kwh"] for s in slots)
    remaining_discharge_budget_kwh = max(0.0, allocated_total_kwh - current_slot["allocated_kwh"])
    slots_left = max(1, len(slots))
    required_discharge_power_w = (allocated_total_kwh / (slots_left * SLOT_H)) * 1000.0
    coverage_ratio = 0.0 if valuable_load_kwh <= 1e-6 else min(1.5, usable_energy_ac_kwh / valuable_load_kwh)
    current_fill_ratio = 0.0 if current_slot["absorbable_kwh"] <= 1e-6 else current_slot["allocated_kwh"] / current_slot["absorbable_kwh"]

    if current_allocated_power_w <= 1.0:
        mode = "discharge_blocked" if any(s["allocated_kwh"] > 1e-6 for s in slots[1:]) else "idle"
    elif current_is_local_peak or (slot_value_score >= 0.85 and coverage_ratio < 0.95) or current_fill_ratio >= 0.98:
        mode = "discharge_push"
    else:
        mode = "discharge_limited"

    return {
        "mode": mode,
        "next_charge_window_start": next_charge_window_start,
        "remaining_discharge_budget_kwh": round(remaining_discharge_budget_kwh, 3),
        "expected_net_load_until_charge_kwh": round(expected_net_load_until_charge_kwh, 3),
        "required_discharge_power_w": round(required_discharge_power_w, 1),
        "slot_value_score": round(slot_value_score, 3),
        "current_is_local_peak": current_is_local_peak,
        "coverage_ratio": round(coverage_ratio, 3),
        "current_allocated_power_w": round(current_allocated_power_w, 1),
        "valuable_load_kwh": round(valuable_load_kwh, 3),
        "deferral_penalty_ct": round(deferral_penalty_ct, 3),
    }


def split_profile(points, date_str):
    arr = [p for p in points if p.get("date") == date_str]
    return {
        "price": [[p["ts_ms"], p["price_ct"]] for p in arr],
        "soc": [[p["ts_ms"], p["soc_pct"]] for p in arr],
        "power": [[p["ts_ms"], p["power_w"]] for p in arr],
        "charge_power": [[p["ts_ms"], p["charge_fc_w"]] for p in arr],
        "discharge_power": [[p["ts_ms"], p["discharge_fc_w"]] for p in arr],
        "discharge_budget_kwh": [[p["ts_ms"], p.get("discharge_budget_kwh", 0.0)] for p in arr],
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


def derive_planned_dispatch(first_plan, discharge_ctx):
    if not first_plan:
        return "idle", 0

    plan_mode = first_plan.get("mode", "idle")
    plan_power = int(round(abs(float(first_plan.get("power_w", 0.0)))))

    if plan_mode == "charge":
        return "charge_grid", plan_power
    if plan_mode == "discharge":
        return discharge_ctx.get("mode", "discharge_limited"), plan_power
    if discharge_ctx.get("mode") == "discharge_blocked" and float(discharge_ctx.get("remaining_discharge_budget_kwh", 0.0) or 0.0) > 0.0:
        return "discharge_blocked", 0
    return "idle", 0


def mode_to_plan_seed(mode_str):
    mode_str = str(mode_str or "idle")
    if mode_str in ("charge_grid", "charge_pv_surplus"):
        return 1
    if mode_str.startswith("discharge_"):
        return -1
    return 0


def series_value_at_or_before(series, ts):
    if not series:
        return None
    ts_arr = [float(t) for t, _ in series]
    idx = bisect.bisect_right(ts_arr, float(ts)) - 1
    if idx < 0:
        return None
    return float(series[idx][1])


def build_series_index(series):
    if not series:
        return [], []
    ts_arr = [float(t) for t, _ in series]
    val_arr = [float(v) for _, v in series]
    return ts_arr, val_arr


def indexed_value_at_or_before(index, ts):
    ts_arr, val_arr = index
    if not ts_arr:
        return None
    idx = bisect.bisect_right(ts_arr, float(ts)) - 1
    if idx < 0:
        return None
    return val_arr[idx]


def update_actual_savings_incremental(data, now_ts):
    """
    Event-driven incremental savings accounting.

    Instead of re-scanning the full 21-day DB history on every run, we track
    the last-seen battery counter values (kWh) and process only the delta that
    occurred since the previous run.  Each delta is multiplied by the
    contemporaneous Tibber price and written permanently into actual_daily_savings.

    This eliminates all window-shift artefacts: every charge/discharge event is
    stamped exactly once at the price valid at that moment and is never
    re-evaluated or evicted.

    Migration / first run
    ---------------------
    When 'savings_tracker' is absent the function initialises the baseline from
    the current counter values.  The existing actual_daily_savings dict (built by
    the previous full-scan code) is preserved as-is so the lifetime total
    continues from the current value without a reset.

    State keys used
    ---------------
    data['savings_tracker']          – {'last_input_kwh', 'last_output_kwh', 'last_ts'}
    data['actual_daily_savings']     – {date_iso: {charge_grid_kwh, charge_pv_kwh,
                                         discharge_used_kwh, charge_cost_eur,
                                         discharge_credit_eur, saving_eur}}
    data['actual_savings_archived_eur'] – permanent accumulator for days that have
                                          been trimmed from actual_daily_savings
    """
    local_now = dt.datetime.now(OPEN_METEO_TZ)
    today = local_now.date().isoformat()

    tracker  = data.setdefault("savings_tracker", {})
    daily    = data.setdefault("actual_daily_savings", {})
    archived = float(data.get("actual_savings_archived_eur", 0.0))

    last_ts         = float(tracker.get("last_ts", 0.0))
    last_input_kwh  = tracker.get("last_input_kwh")
    last_output_kwh = tracker.get("last_output_kwh")
    # first run = tracker has never been committed (last_ts == 0)
    is_first_run    = (last_ts == 0.0)

    # One-time backfill: when migrating from the old full-scan code the first-run
    # set the counter baseline to whatever value was current at that moment, so
    # events that occurred between CEST midnight and first-run time were silently
    # lost.  We detect this via a flag and re-scan the last 2 CEST days once.
    needs_backfill = (
        not is_first_run
        and not tracker.get("savings_backfill_v1_done")
    )

    # CEST midnight of today — used to always do a full rescan of today's events.
    # This ensures HA restarts or script gaps never leave holes in today's data.
    today_midnight_ts = dt.datetime(
        local_now.year, local_now.month, local_now.day, tzinfo=OPEN_METEO_TZ
    ).timestamp()

    # Fetch sensor data:
    #   first run  → 24 h window to find current counter values
    #   backfill   → 2 full CEST days so we catch all missed events
    #   normal run → from the earlier of (last_ts-120 s) or CEST midnight today,
    #                so today's events are always fully covered even after a gap
    if is_first_run:
        query_from = now_ts - 86400.0
    elif needs_backfill:
        cest_2d_ago = dt.datetime(
            local_now.year, local_now.month, local_now.day, tzinfo=OPEN_METEO_TZ
        ) - dt.timedelta(days=2)
        query_from = cest_2d_ago.timestamp()
    else:
        query_from = min(last_ts - 120.0, today_midnight_ts)

    series_map = fetch_sensor_series_many(
        [
            E_PRICE_EUR,
            E_BATTERY_INPUT_ENERGY,
            E_BATTERY_OUTPUT_ENERGY,
            E_GRID_IMPORT,
            E_GRID_EXPORT,
            E_BATTERY_POWER,
        ],
        query_from,
    )
    price_series         = series_map.get(E_PRICE_EUR, [])
    input_series         = series_map.get(E_BATTERY_INPUT_ENERGY, [])
    output_series        = series_map.get(E_BATTERY_OUTPUT_ENERGY, [])
    grid_import_series   = series_map.get(E_GRID_IMPORT, [])
    grid_export_series   = series_map.get(E_GRID_EXPORT, [])
    battery_power_series = series_map.get(E_BATTERY_POWER, [])
    tibber_price_ts, tibber_price_vals = build_tibber_price_index(local_date_set_between(query_from, now_ts))

    # ------------------------------------------------------------------ helpers
    def _make_day_rec():
        return {"charge_grid_kwh": 0.0, "charge_pv_kwh": 0.0,
                "discharge_used_kwh": 0.0, "charge_cost_eur": 0.0,
                "discharge_credit_eur": 0.0, "saving_eur": 0.0}

    def _ensure_day(day):
        return daily.setdefault(day, _make_day_rec())

    def _price_eur(ts, tibber_ts, tibber_vals, price_index=None, price_is_ct=False):
        p = tibber_price_eur_at(ts, tibber_ts, tibber_vals)
        if p is not None:
            return p
        v = indexed_value_at_or_before(price_index, ts) if price_index is not None else None
        if v is None:
            return None
        return (float(v) / 100.0) if price_is_ct else float(v)

    def _charge_split(delta_kwh, ts, gi_idx, ge_idx, bat_idx):
        """Estimate what fraction of a charge delta came from PV vs grid."""
        if delta_kwh <= 0:
            return 0.0, 0.0
        gi  = max(0.0, float(indexed_value_at_or_before(gi_idx,  ts) or 0.0))
        ge  = max(0.0, float(indexed_value_at_or_before(ge_idx,  ts) or 0.0))
        bat = float(indexed_value_at_or_before(bat_idx, ts) or 0.0)
        charge_w = max(0.0, -bat)
        export_wo_bat_w = max(0.0, -(gi - ge + bat))
        if charge_w <= 1.0:
            pv_ratio = 1.0 if export_wo_bat_w > 1.0 else 0.0
        else:
            pv_ratio = min(1.0, export_wo_bat_w / charge_w)
        pv_kwh   = delta_kwh * pv_ratio
        grid_kwh = max(0.0, delta_kwh - pv_kwh)
        return grid_kwh, pv_kwh

    # -------------------------------------------------- first-run initialisation
    if is_first_run:
        # Set baseline counter values so future runs can compute deltas.
        # Do NOT process any past events – the existing actual_daily_savings
        # (built by the old full-scan code) already covers history.
        tracker["last_input_kwh"]  = float(input_series[-1][1])  if input_series  else None
        tracker["last_output_kwh"] = float(output_series[-1][1]) if output_series else None
        tracker["last_ts"] = now_ts
        # Fall through to metrics computation below (nothing new to add).
    else:
        # -------------------------------------------- incremental event processing
            if not tibber_price_ts and not price_series:
                # No price data available; skip this run without moving the tracker.
                pass
            else:
                price_vals   = [float(v) for _, v in price_series]
                price_median = statistics.median(price_vals) if price_vals else 0.0
                price_is_ct  = price_median > 2.0
            price_idx  = build_series_index(price_series)
            gi_idx     = build_series_index(grid_import_series)
            ge_idx     = build_series_index(grid_export_series)
            bat_idx    = build_series_index(battery_power_series)
            max_delta  = 8.0  # kWh guardrail against counter resets / corrupt jumps

            if needs_backfill:
                # Re-process the last 2 CEST days from scratch.
                # Clear those days' accumulated data so we don't double-count.
                yesterday_iso = (local_now.date() - dt.timedelta(days=1)).isoformat()
                backfill_days = {today, yesterday_iso}
                for _d in backfill_days:
                    daily.pop(_d, None)
                # Use ts_cutoff=0 so all events in the query window are processed;
                # use first series value as delta baseline (not the tracker baseline
                # which was set at post-migration init time).
                input_prev_init    = float(input_series[0][1])  if input_series  else None
                output_prev_init   = float(output_series[0][1]) if output_series else None
                tracker["savings_backfill_v1_done"] = True
            else:
                backfill_days    = None      # None → no day filter; today handled below
                input_prev_init  = float(input_series[0][1]) if input_series else None
                output_prev_init = float(output_series[0][1]) if output_series else None

                # Always recompute today from CEST midnight so HA restarts and gaps
                # never leave holes.  The loop below will repopulate daily[today].
                daily.pop(today, None)

            # Effective ts_cutoff for the event loop:
            #   • backfill: 0 (process everything in the 2-day window)
            #   • normal  : events on PAST days use last_ts (skip already done);
            #               events on TODAY always get processed (from midnight)
            #   The per-event logic below enforces this with a combined condition.
            loop_ts_cutoff = 0.0 if needs_backfill else last_ts

            # -- charge (battery_input) -----------------------------------------
            if needs_backfill:
                prev_v = input_prev_init
            else:
                # Skip if counter baseline was unavailable at initialisation (battery
                # was idle for >24 h then); on the next run the series will contain
                # the post-init values and we use the last series value as baseline.
                if input_prev_init is not None:
                    prev_v = input_prev_init
                else:
                    if last_input_kwh is None and input_series:
                        last_input_kwh = float(input_series[0][1])
                    prev_v = float(last_input_kwh) if last_input_kwh is not None else None
            for ts, val in (input_series if prev_v is not None else []):
                ts, val = float(ts), float(val)
                delta = val - prev_v
                prev_v = val
                day = local_dt_from_ts(ts).date().isoformat()
                # Skip already-processed events, but always process today's events
                if ts <= loop_ts_cutoff and day != today:
                    continue
                if delta <= 0 or delta > max_delta:
                    continue
                if backfill_days is not None and day not in backfill_days:
                    continue                # backfill only touches cleared days
                p = _price_eur(ts, tibber_price_ts, tibber_price_vals, price_idx, price_is_ct)
                if p is None:
                    continue
                rec = _ensure_day(day)
                grid_kwh, pv_kwh = _charge_split(delta, ts, gi_idx, ge_idx, bat_idx)
                cost = grid_kwh * p
                rec["charge_grid_kwh"]  += grid_kwh
                rec["charge_pv_kwh"]    += pv_kwh
                rec["charge_cost_eur"]  += cost
                rec["saving_eur"]       -= cost

            # -- discharge (battery_output) -------------------------------------
            # Product decision for this release: every measured battery-output
            # counter delta is credited at the current import price. This is a
            # deliberately simple gross-savings metric; it does not yet remove
            # battery export or EV consumption. Keep this explicit until the
            # metric is intentionally upgraded to avoided-grid-import savings.
            if needs_backfill:
                prev_v = output_prev_init
            else:
                if output_prev_init is not None:
                    prev_v = output_prev_init
                else:
                    if last_output_kwh is None and output_series:
                        last_output_kwh = float(output_series[0][1])
                    prev_v = float(last_output_kwh) if last_output_kwh is not None else None
            for ts, val in (output_series if prev_v is not None else []):
                ts, val = float(ts), float(val)
                delta = val - prev_v
                prev_v = val
                day = local_dt_from_ts(ts).date().isoformat()
                # Skip already-processed events, but always process today's events
                if ts <= loop_ts_cutoff and day != today:
                    continue
                if delta <= 0 or delta > max_delta:
                    continue
                if backfill_days is not None and day not in backfill_days:
                    continue
                p = _price_eur(ts, tibber_price_ts, tibber_price_vals, price_idx, price_is_ct)
                if p is None:
                    continue
                rec = _ensure_day(day)
                credit = delta * p
                rec["discharge_used_kwh"]    += delta
                rec["discharge_credit_eur"]  += credit
                rec["saving_eur"]            += credit

            # advance tracker to current counter values
            if input_series:
                tracker["last_input_kwh"]  = float(input_series[-1][1])
            if output_series:
                tracker["last_output_kwh"] = float(output_series[-1][1])
            tracker["last_ts"] = now_ts

    # --------------------------------------------------------- housekeeping
    # Round all accumulated values.
    for rec in daily.values():
        for key in ("charge_grid_kwh", "charge_pv_kwh", "discharge_used_kwh",
                    "charge_cost_eur", "discharge_credit_eur", "saving_eur"):
            rec[key] = round(float(rec.get(key, 0.0)), 4)

    # Trim entries older than HISTORY_DAYS; accumulate their saving into the
    # permanent archive so lifetime never loses old data.
    trim_before = (local_now.date() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    for day in sorted(k for k in list(daily) if k < trim_before):
        archived += float(daily.pop(day, {}).get("saving_eur", 0.0))
    data["actual_savings_archived_eur"] = round(archived, 4)

    # --------------------------------------------------------- output metrics
    today_saving = float(daily.get(today, {}).get("saving_eur", 0.0))

    lifetime = archived + sum(float(v.get("saving_eur", 0.0)) for v in daily.values())

    return (
        daily,
        round(today_saving, 3),
        round(lifetime, 3),
    )


def main():
    global SOC_MIN, MIN_E_KWH
    now = dt.datetime.now(dt.timezone.utc)
    now_ts = now.timestamp()
    local_now = dt.datetime.now(OPEN_METEO_TZ)
    today = local_now.date().isoformat()
    tomorrow = (local_now.date() + dt.timedelta(days=1)).isoformat()

    data = load_state()
    inputs = collect_inputs()
    if inputs.get("error"):
        out = fallback_output("no_price", inputs["error"], data, now.isoformat())
        print(json.dumps(out, ensure_ascii=False))
        return

    p_now = inputs["p_now"]
    p_future_max = inputs["p_future_max"]
    grid_import_w = inputs["grid_import_w"]
    grid_export_w = inputs["grid_export_w"]
    pv_w = inputs["pv_w"]
    wallbox_w = inputs["wallbox_w"]
    bat_in_out_w = inputs["bat_in_out_w"]
    house_load_total_w = max(0.0, grid_import_w + pv_w + bat_in_out_w - grid_export_w)
    house_load_w = max(0.0, house_load_total_w - wallbox_w)
    soc = inputs["soc"]
    if soc is not None and 0.0 <= float(soc) <= 100.0:
        soc = float(soc)
        data["last_known_soc_pct"] = soc
    else:
        persisted_soc = data.get("last_known_soc_pct")
        try:
            persisted_soc = float(persisted_soc)
        except (TypeError, ValueError):
            persisted_soc = None
        if persisted_soc is None:
            for sample in reversed(data.get("samples") or []):
                try:
                    sample_soc = float(sample.get("soc"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if 0.0 <= sample_soc <= 100.0:
                    persisted_soc = sample_soc
                    break
        soc = persisted_soc if persisted_soc is not None and 0.0 <= persisted_soc <= 100.0 else None
        if soc is not None:
            data["last_known_soc_pct"] = soc
    soc_min_pct = clamp(float(inputs.get("soc_min_pct", SOC_MIN)), 0.0, 40.0)
    SOC_MIN = soc_min_pct
    MIN_E_KWH = CAP_KWH * (SOC_MIN / 100.0)
    hp_w = inputs["hp_w"]
    pv_raw_kwh = inputs["pv_raw_kwh"]
    pv_tomorrow_kwh = inputs["pv_tomorrow_kwh"]
    cloud = inputs["cloud"]
    rad = inputs["rad"]

    if len(data.get("samples", [])) < 120:
        data["samples"] = bootstrap_samples_from_db(now_ts, days=21)

    data["pv_bias_slots"] = normalize_slot_biases(data.get("pv_bias_slots"), 0.5, 1.6)
    data["load_bias_slots"] = normalize_slot_biases(data.get("load_bias_slots"), 0.6, 1.6)

    data["samples"].append(
        {
            "ts": now_ts,
            "load_w": house_load_w,  # canonical forecast key = house load
            "house_w": house_load_w,
            "house_total_w": house_load_total_w,
            "wallbox_w": wallbox_w,
            "grid_import_w": grid_import_w,
            "grid_export_w": grid_export_w,
                "pv_w": pv_w,
                "bat_in_out_w": bat_in_out_w,
                "hp_w": hp_w,
            "price_ct": p_now,
            "soc": soc if soc is not None else -1,
        }
    )

    cutoff = now_ts - HISTORY_DAYS * 86400
    data["samples"] = [x for x in data["samples"] if x.get("ts", 0) >= cutoff][-12000:]
    (
        actual_daily_savings,
        actual_today_saving,
        actual_savings_lifetime_eur,
    ) = update_actual_savings_incremental(data, now_ts)

    # actual_daily_savings is maintained inside update_actual_savings_incremental
    # via data["actual_daily_savings"] directly; retrieve for output helpers.
    actual_daily_savings = data["actual_daily_savings"]
    actual_inventory_deliverable_kwh = None
    actual_inventory_cost_ct_per_kwh = None
    actual_today_stats = actual_daily_savings.get(today, {})

    # improved load forecast: average next 4 slots from historical slot model
    next_slots = [local_now + dt.timedelta(minutes=15 * i) for i in range(1, 5)]
    load_bias = clamp(float(data.get("load_bias", 1.0)), 0.6, 1.6)
    load_forecast_ws = []
    for t in next_slots:
        slot = slot_index_for_dt(t)
        load_forecast_ws.append(
            forecast_load_w_for_slot(
                data["samples"],
                t,
                max(200.0, house_load_w),
                hp_w,
                now_dt=local_now,
                load_bias=load_bias,
                slot_bias=data["load_bias_slots"][slot],
            )
        )
    load_fc_kwh = sum(load_forecast_ws) / 1000.0 * SLOT_H

    weather_factor = weather_factor_from_cloud_rad(cloud, rad)
    pv_bias = clamp(float(data.get("pv_bias", 1.0)), 0.5, 1.4)
    pv_corr_kwh = max(0.0, pv_raw_kwh * pv_bias * weather_factor)

    net_kwh = max(0.0, load_fc_kwh - pv_corr_kwh)
    pv_surplus_w = real_charge_follow_surplus_w(grid_import_w, grid_export_w, bat_in_out_w)
    # Net import that would exist without battery and EV influence.
    net_no_battery_no_ev_now_w = net_no_battery_no_ev_w(grid_import_w, grid_export_w, bat_in_out_w, wallbox_w)
    net_no_battery_with_ev_now_w = net_no_battery_with_ev_w(grid_import_w, grid_export_w, bat_in_out_w)
    net_now_w = max(0.0, net_no_battery_no_ev_now_w)
    pv_surplus_stable, pv_surplus_avg = recent_surplus_stable(data["samples"])
    rte_break_even_ct = (p_now / ETA_RT) + MIN_MARGIN_CT if p_now is not None else None
    expected_spread_ct = (p_future_max * ETA_RT) - p_now if p_now is not None else None

    mode = "idle"
    rec_w = 0
    reason = "15min Tibber plan"

    due = [p for p in data["predictions"] if p.get("target_ts", 0) <= now_ts]
    data["predictions"] = [p for p in data["predictions"] if p.get("target_ts", 0) > now_ts][-1200:]

    for pred in due:
        end_ts = pred["target_ts"]
        start_ts = end_ts - 3600
        end_local = dt.datetime.fromtimestamp(end_ts, tz=local_now.tzinfo)
        slot = slot_index_for_dt(end_local)
        pv_avg = avg_power(data["samples"], start_ts, end_ts, "pv_w")
        load_avg = avg_power(data["samples"], start_ts, end_ts, "load_w")
        price_target = avg_power(data["samples"], end_ts - 900, end_ts + 900, "price_ct")
        if pv_avg is None or load_avg is None or price_target is None:
            continue

        pv_actual = max(0.0, pv_avg) / 1000.0
        load_actual = max(0.0, load_avg) / 1000.0
        pv_err = abs(pred.get("pv_pred_kwh", 0.0) - pv_actual)
        load_err = abs(pred.get("load_pred_kwh", 0.0) - load_actual)

        # Online calibration so forecast improves over time.
        pv_pred = max(0.05, float(pred.get("pv_pred_kwh", 0.0)))
        if pv_actual > 0.02:
            pv_ratio = clamp(pv_actual / pv_pred, 0.7, 1.3)
            data["pv_bias"] = clamp((1.0 - BIAS_ALPHA) * float(data.get("pv_bias", 1.0)) + BIAS_ALPHA * pv_ratio, 0.5, 1.6)
            old = data["pv_bias_slots"][slot]
            data["pv_bias_slots"][slot] = clamp((1.0 - SLOT_BIAS_ALPHA) * old + SLOT_BIAS_ALPHA * pv_ratio, 0.5, 1.6)

        load_pred = max(0.2, float(pred.get("load_pred_kwh", 0.0)))
        load_ratio = clamp(load_actual / load_pred, 0.75, 1.25)
        data["load_bias"] = clamp((1.0 - BIAS_ALPHA) * float(data.get("load_bias", 1.0)) + BIAS_ALPHA * load_ratio, 0.6, 1.6)
        old_l = data["load_bias_slots"][slot]
        data["load_bias_slots"][slot] = clamp((1.0 - SLOT_BIAS_ALPHA) * old_l + SLOT_BIAS_ALPHA * load_ratio, 0.6, 1.6)

        success = True
        if pred.get("mode") == "charge_grid":
            success = (price_target * ETA_RT) > pred.get("price_ct", 0.0)
        elif str(pred.get("mode", "")).startswith("discharge_"):
            success = (pred.get("price_ct", 0.0) * ETA_RT) > price_target

        data["backtests"].append(
            {
                "ts": end_ts,
                "pv_mae": pv_err,
                "load_mae": load_err,
                "success": bool(success),
                "pv_bias_after": round(float(data.get("pv_bias", 1.0)), 4),
                "load_bias_after": round(float(data.get("load_bias", 1.0)), 4),
            }
        )

    bt_cutoff = now_ts - HISTORY_DAYS * 86400
    data["backtests"] = [b for b in data["backtests"] if b.get("ts", 0) >= bt_cutoff][-8000:]

    bt24 = [b for b in data["backtests"] if b.get("ts", 0) >= now_ts - 86400]
    bt7d = [b for b in data["backtests"] if b.get("ts", 0) >= now_ts - 7 * 86400]

    def mae(items, key):
        return (sum(i.get(key, 0.0) for i in items) / len(items)) if items else None

    hit24 = (100.0 * sum(1 for x in bt24 if x.get("success")) / len(bt24)) if bt24 else None

    eex_days = get_eex_day_context(data, local_now)
    intervals_all = read_tibber_intervals_for_dates({today, tomorrow})
    intervals_all, tomorrow_price_source = apply_eex_proxy_prices(
        intervals_all,
        eex_days,
        local_now.date(),
        local_now.date() + dt.timedelta(days=1),
    )
    now_floor = floor_to_quarter(local_now)
    now_ts_ms = int(now_ts * 1000)
    intervals = [it for it in intervals_all if it["dt"] >= now_floor]
    intervals = intervals[: int(math.ceil(PLANNING_HORIZON_H / SLOT_H))]
    if soc is not None:
        start_e = clamp(CAP_KWH * soc / 100.0, MIN_E_KWH, MAX_E_KWH)
        initial_plan_mode = 0
    else:
        start_e = advance_virtual_energy(data, now_ts)
        initial_plan_mode = mode_to_plan_seed(data.get("virtual_last_mode"))

    load_bias_plan = clamp(float(data.get("load_bias", load_bias)), 0.6, 1.6)
    inventory_discharge_floor_ct = None
    if actual_inventory_cost_ct_per_kwh is None:
        actual_today_charge_in_kwh = float(actual_today_stats.get("charge_grid_kwh", 0.0)) + float(actual_today_stats.get("charge_pv_kwh", 0.0))
        actual_today_charge_cost_eur = float(actual_today_stats.get("charge_cost_eur", 0.0))
        if actual_today_charge_in_kwh > 0.25:
            actual_inventory_cost_ct_per_kwh = (
                actual_today_charge_cost_eur / max(1e-9, actual_today_charge_in_kwh * ETA_RT)
            ) * 100.0
    if actual_inventory_cost_ct_per_kwh is not None:
        inventory_discharge_floor_ct = actual_inventory_cost_ct_per_kwh + MIN_MARGIN_CT
    plan = build_virtual_plan(
        intervals,
        data["samples"],
        start_e,
        weather_factor,
        pv_tomorrow_kwh,
        load_bias_plan,
        data["load_bias_slots"],
        data["pv_bias_slots"],
        initial_plan_mode,
        (inputs.get("weather") or {}).get("hourly"),
        pv_now_actual_w=max(0.0, pv_w),
        now_local=local_now,
        pv_global_bias=pv_bias,
        eex_days=eex_days,
        inventory_discharge_floor_ct=inventory_discharge_floor_ct,
        shadow_history=_SHADOW_FEATURE_HISTORY,
    )
    forecast_diagnostics = plan.get("forecast_diagnostics", {})
    future_points = plan["points"]
    usable_energy_ac_kwh = max(0.0, start_e - MIN_E_KWH) * ETA_D
    discharge_ctx = classify_discharge_mode(future_points, p_now, usable_energy_ac_kwh)
    planned_mode = mode
    planned_power_w = rec_w
    if future_points:
        planned_mode, planned_power_w = derive_planned_dispatch(future_points[0], discharge_ctx)

    mode = planned_mode
    rec_w = planned_power_w
    if mode == "discharge_push":
        rec_w = int(clamp(max(0.0, net_now_w), 0.0, MAX_DISCHARGE_P_W))
        if rec_w <= 0:
            mode = "discharge_blocked"
            rec_w = 0
            reason = "push window but no live net load"
        elif rec_w != planned_power_w:
            reason = "push window against live net load"
    elif mode == "discharge_limited":
        rec_w = int(clamp(min(float(planned_power_w), max(0.0, net_now_w)), 0.0, MAX_DISCHARGE_P_W))
        if rec_w <= 0:
            mode = "discharge_blocked"
            rec_w = 0
            reason = "limited discharge blocked by live net load"
        elif rec_w < planned_power_w:
            reason = "limited discharge capped by live net load"
    elif mode == "discharge_blocked":
        rec_w = 0
        reason = "battery reserved for later higher-value slots"
    elif mode in ("charge_grid", "charge_pv_surplus"):
        rec_w = int(clamp(float(planned_power_w), 0.0, MAX_CHARGE_P_W))
        if pv_surplus_stable and pv_surplus_w > 0 and start_e < (MAX_E_KWH - 0.05):
            mode = "charge_follow"
            rec_w = int(clamp(float(pv_surplus_w), 0.0, MAX_CHARGE_P_W))
            reason = "price plan + pv surplus follow"

    if (
        mode in ("idle", "discharge_blocked")
        and pv_surplus_stable
        and pv_surplus_w >= PV_SURPLUS_MIN_SAMPLE_W
        and start_e < (MAX_E_KWH - 0.05)
    ):
        mode = "charge_follow"
        rec_w = int(clamp(float(pv_surplus_w), 0.0, MAX_CHARGE_P_W))
        reason = "stable pv surplus follow"

    if soc is None:
        reason = reason + " (virtual battery mode)"
    data["predictions"].append(
        {
            "target_ts": now_ts + 3600,
            "mode": mode,
            "price_ct": p_now,
            "pv_pred_kwh": pv_corr_kwh,
            "load_pred_kwh": load_fc_kwh,
            "pv_bias_used": pv_bias,
            "load_bias_used": load_bias,
        }
    )
    actual_points = data.get("virtual_trace", [])
    if soc is None:
        append_virtual_trace(data, int(now_ts * 1000), today, (start_e / CAP_KWH) * 100.0, mode, rec_w)
        actual_points = data.get("virtual_trace", [])
        data["virtual_last_ts"] = now_ts
        data["virtual_last_mode"] = mode
        data["virtual_last_power_w"] = rec_w
        data["virtual_energy_kwh"] = start_e

    (
        forecast_today,
        forecast_tomorrow,
        profile_today,
        profile_tomorrow,
    ) = build_published_plan_profiles(
        actual_points,
        future_points,
        today,
        tomorrow,
        now_ts_ms,
    )
    profile_today["price"] = build_price_profile(intervals_all, today)
    profile_tomorrow["price"] = build_price_profile(intervals_all, tomorrow)
    price_obs = compute_price_quantiles(data["samples"], local_now, p_now, profile_tomorrow["price"])
    for k in ("pv_fc_power", "grid_import_fc_power", "grid_export_fc_power", "grid_net_fc_power"):
        profile_today[k] = forecast_today[k]
        profile_tomorrow[k] = forecast_tomorrow[k]

    save_today = plan.get("today", {}).get("saving_eur", 0.0) or 0.0
    save_tom = plan.get("tomorrow", {}).get("saving_eur", 0.0) or 0.0

    data["daily_savings"][today] = round(save_today, 3)
    cumulative = 0.0
    for k, v in data["daily_savings"].items():
        if k <= today:
            cumulative += float(v)
    keys = sorted(data["daily_savings"].keys())
    if len(keys) > 120:
        for k in keys[:-120]:
            data["daily_savings"].pop(k, None)

    slot_now = slot_index_for_dt(local_now)
    eex_today = eex_days.get(today, {})
    eex_tomorrow = eex_days.get(tomorrow, {})
    day_after = (local_now.date() + dt.timedelta(days=2)).isoformat()
    eex_day_after = eex_days.get(day_after, {})
    out = {
        "mode": mode,
        "planned_mode": planned_mode,
        "planned_power_w": planned_power_w,
        "recommended_power_w": rec_w,
        "planned_charge_power_w": int(planned_power_w if planned_mode in ("charge_grid", "charge_pv_surplus", "charge_follow") else 0),
        "planned_discharge_power_w": int(planned_power_w if planned_mode.startswith("discharge_") else 0),
        "recommended_charge_power_w": int(rec_w if mode in ("charge_grid", "charge_pv_surplus", "charge_follow") else 0),
        "recommended_discharge_power_w": int(rec_w if mode.startswith("discharge_") else 0),
        "discharge_mode_detail": discharge_ctx["mode"],
        "next_charge_window_start_ts": discharge_ctx["next_charge_window_start"],
        "remaining_discharge_budget_kwh": discharge_ctx["remaining_discharge_budget_kwh"],
        "expected_net_load_until_charge_kwh": discharge_ctx["expected_net_load_until_charge_kwh"],
        "required_discharge_power_w": discharge_ctx["required_discharge_power_w"],
        "slot_value_score": discharge_ctx["slot_value_score"],
        "current_is_local_peak": discharge_ctx["current_is_local_peak"],
        "coverage_ratio": discharge_ctx["coverage_ratio"],
        "current_allocated_power_w": discharge_ctx["current_allocated_power_w"],
        "valuable_load_kwh": discharge_ctx["valuable_load_kwh"],
        "reason": reason,
        "expected_spread_ct": round(expected_spread_ct, 2) if expected_spread_ct is not None else None,
        "rte_break_even_ct": round(rte_break_even_ct, 2) if rte_break_even_ct is not None else None,
        "load_forecast_next_1h_kwh": round(load_fc_kwh, 3),
        "pv_forecast_raw_next_1h_kwh": round(pv_raw_kwh, 3),
        "pv_forecast_corrected_next_1h_kwh": round(pv_corr_kwh, 3),
        "net_load_forecast_next_1h_kwh": round(net_kwh, 3),
        "grid_import_forecast_next_1h_kwh": round(max(0.0, net_kwh), 3),
        "grid_export_forecast_next_1h_kwh": round(max(0.0, pv_corr_kwh - load_fc_kwh), 3),
        "pv_surplus_now_w": int(pv_surplus_w),
        "pv_surplus_avg_20m_w": round(pv_surplus_avg, 1),
        "pv_surplus_stable": bool(pv_surplus_stable),
        "heatpump_power_now_w": int(max(0.0, hp_w)),
        "grid_import_actual_now_w": int(max(0.0, grid_import_w)),
        "grid_export_actual_now_w": int(max(0.0, grid_export_w)),
        "grid_net_actual_now_w": int(max(0.0, grid_import_w) - max(0.0, grid_export_w)),
        "grid_net_no_battery_no_ev_now_w": int(round(net_no_battery_no_ev_now_w)),
        "grid_net_no_battery_with_ev_now_w": int(round(net_no_battery_with_ev_now_w)),
        "house_load_actual_now_w": int(max(0.0, house_load_w)),
        "house_load_total_actual_now_w": int(max(0.0, house_load_total_w)),
        "wallbox_actual_now_w": int(max(0.0, wallbox_w)),
        "pv_generation_actual_now_w": int(max(0.0, pv_w)),
        "backtest_mae_pv_24h_kwh": round(mae(bt24, "pv_mae"), 3) if bt24 else None,
        "backtest_mae_load_24h_kwh": round(mae(bt24, "load_mae"), 3) if bt24 else None,
        "backtest_mae_pv_7d_kwh": round(mae(bt7d, "pv_mae"), 3) if bt7d else None,
        "backtest_mae_load_7d_kwh": round(mae(bt7d, "load_mae"), 3) if bt7d else None,
        "backtest_hit_rate_24h_pct": round(hit24, 1) if hit24 is not None else None,
        "weather_factor": round(weather_factor, 3),
        "script_version": SCRIPT_VERSION,
        "forecast_source": forecast_diagnostics.get("source"),
        "forecast_slot_count": forecast_diagnostics.get("slot_count"),
        "forecast_runtime_ms": forecast_diagnostics.get("runtime_ms"),
        "forecast_model_version": forecast_diagnostics.get("model_version"),
        "pv_bias": round(data.get("pv_bias", 1.0), 3),
        "load_bias": round(data.get("load_bias", 1.0), 3),
        "pv_bias_slot_now": round(float(data["pv_bias_slots"][slot_now]), 3),
        "load_bias_slot_now": round(float(data["load_bias_slots"][slot_now]), 3),
        "virtual_soc_start_pct": round((start_e / CAP_KWH) * 100.0, 2),
        "soc_min_pct": round(SOC_MIN, 1),
        "virtual_soc_end_tomorrow_pct": plan.get("end_soc", 50.0),
        "price_low_ct": plan.get("price_stats", {}).get("p_low"),
        "price_high_ct": plan.get("price_stats", {}).get("p_high"),
        "price_avg_ct": plan.get("price_stats", {}).get("avg"),
        "price_min_ct": plan.get("price_stats", {}).get("min"),
        "price_max_ct": plan.get("price_stats", {}).get("max"),
        "terminal_value_ct": plan.get("price_stats", {}).get("terminal_value_ct"),
        "tomorrow_day_min_rank": plan.get("price_stats", {}).get("tomorrow_min_rank"),
        "discharge_floor_ct": plan.get("price_stats", {}).get("discharge_floor_ct"),
        "cheap_anchor_ct": plan.get("price_stats", {}).get("cheap_anchor_ct"),
        "cheap_anchor_rank": plan.get("price_stats", {}).get("cheap_anchor_rank"),
        "price_slot_median_ct": price_obs["current_slot_median_ct"],
        "price_slot_q20_ct": price_obs["current_slot_q20_ct"],
        "price_slot_q80_ct": price_obs["current_slot_q80_ct"],
        "price_slot_rank": price_obs["current_slot_rank"],
        "price_tomorrow_min_ct": price_obs["tomorrow_min_price_ct"],
        "price_tomorrow_min_rank": price_obs["tomorrow_min_rank"],
        "price_tomorrow_source": tomorrow_price_source,
        "eex_base_today_ct": eex_today.get("base", {}).get("settl_ct_kwh"),
        "eex_peak_today_ct": eex_today.get("peak", {}).get("settl_ct_kwh"),
        "eex_spread_today_ct": eex_today.get("spread_ct_kwh"),
        "eex_trade_date_today": eex_today.get("base", {}).get("trade_date") or eex_today.get("peak", {}).get("trade_date"),
        "eex_base_tomorrow_ct": eex_tomorrow.get("base", {}).get("settl_ct_kwh"),
        "eex_peak_tomorrow_ct": eex_tomorrow.get("peak", {}).get("settl_ct_kwh"),
        "eex_spread_tomorrow_ct": eex_tomorrow.get("spread_ct_kwh"),
        "eex_trade_date_tomorrow": eex_tomorrow.get("base", {}).get("trade_date") or eex_tomorrow.get("peak", {}).get("trade_date"),
        "eex_base_day_after_ct": eex_day_after.get("base", {}).get("settl_ct_kwh"),
        "eex_peak_day_after_ct": eex_day_after.get("peak", {}).get("settl_ct_kwh"),
        "eex_spread_day_after_ct": eex_day_after.get("spread_ct_kwh"),
        "eex_trade_date_day_after": eex_day_after.get("base", {}).get("trade_date") or eex_day_after.get("peak", {}).get("trade_date"),
        "baseline_cost_today_eur": plan.get("daily_costs", {}).get(today, {}).get("base_eur"),
        "optimized_cost_today_eur": plan.get("daily_costs", {}).get(today, {}).get("with_bat_eur"),
        "baseline_cost_tomorrow_eur": plan.get("daily_costs", {}).get(tomorrow, {}).get("base_eur"),
        "optimized_cost_tomorrow_eur": plan.get("daily_costs", {}).get(tomorrow, {}).get("with_bat_eur"),
        "estimated_savings_today_eur": round(save_today, 3),
        "estimated_savings_tomorrow_eur": round(save_tom, 3),
        "estimated_savings_cumulative_eur": round(cumulative, 3),
        "actual_savings_today_eur": round(actual_today_saving, 3),
        "actual_savings_cumulative_eur": actual_savings_lifetime_eur,
        "actual_savings_lifetime_eur": actual_savings_lifetime_eur,
        "actual_inventory_deliverable_kwh": actual_inventory_deliverable_kwh,
        "actual_inventory_cost_ct_per_kwh": actual_inventory_cost_ct_per_kwh,
        "inventory_discharge_floor_ct": round(inventory_discharge_floor_ct, 3) if inventory_discharge_floor_ct is not None else None,
        "actual_battery_charge_grid_today_kwh": float(actual_daily_savings.get(today, {}).get("charge_grid_kwh", 0.0)),
        "actual_battery_charge_pv_today_kwh": float(actual_daily_savings.get(today, {}).get("charge_pv_kwh", 0.0)),
        "actual_battery_discharge_credited_today_kwh": float(actual_daily_savings.get(today, {}).get("discharge_used_kwh", 0.0)),
        "actual_battery_charge_cost_today_eur": float(actual_daily_savings.get(today, {}).get("charge_cost_eur", 0.0)),
        "actual_battery_discharge_credit_today_eur": float(actual_daily_savings.get(today, {}).get("discharge_credit_eur", 0.0)),
        "profile_today_price": profile_today["price"],
        "profile_today_soc": profile_today["soc"],
        "profile_today_power": profile_today["power"],
        "profile_today_charge_power": profile_today["charge_power"],
        "profile_today_discharge_power": profile_today["discharge_power"],
        "profile_today_discharge_budget_kwh": profile_today["discharge_budget_kwh"],
        "profile_today_pv_fc_power": profile_today["pv_fc_power"],
        "profile_today_grid_import_fc_power": profile_today["grid_import_fc_power"],
        "profile_today_grid_export_fc_power": profile_today["grid_export_fc_power"],
        "profile_today_grid_net_fc_power": profile_today["grid_net_fc_power"],
        "profile_tomorrow_price": profile_tomorrow["price"],
        "profile_tomorrow_soc": profile_tomorrow["soc"],
        "profile_tomorrow_power": profile_tomorrow["power"],
        "profile_tomorrow_charge_power": profile_tomorrow["charge_power"],
        "profile_tomorrow_discharge_power": profile_tomorrow["discharge_power"],
        "profile_tomorrow_discharge_budget_kwh": profile_tomorrow["discharge_budget_kwh"],
        "profile_tomorrow_pv_fc_power": profile_tomorrow["pv_fc_power"],
        "profile_tomorrow_grid_import_fc_power": profile_tomorrow["grid_import_fc_power"],
        "profile_tomorrow_grid_export_fc_power": profile_tomorrow["grid_export_fc_power"],
        "profile_tomorrow_grid_net_fc_power": profile_tomorrow["grid_net_fc_power"],
        "profile_48h_pv_fc_power": [
            [p["ts_ms"], p["pv_fc_w"]] for p in future_points
        ],
        "profile_48h_house_fc_power": [
            [p["ts_ms"], p["load_fc_w"]] for p in future_points
        ],
        "profile_48h_charge_fc_power": [
            [p["ts_ms"], p["charge_fc_w"]] for p in future_points
        ],
        "profile_48h_discharge_fc_power": [
            [p["ts_ms"], p["discharge_fc_w"]] for p in future_points
        ],
        "profile_48h_discharge_budget_kwh": [
            [p["ts_ms"], p.get("discharge_budget_kwh", 0.0)]
            for p in future_points
        ],
        "profile_48h_grid_import_fc_power": [
            [p["ts_ms"], p["grid_import_fc_w"]] for p in future_points
        ],
        "profile_48h_grid_export_fc_power": [
            [p["ts_ms"], p["grid_export_fc_w"]] for p in future_points
        ],
        "profile_48h_grid_net_fc_power": [
            [p["ts_ms"], p["grid_net_fc_w"]] for p in future_points
        ],
        "profile_48h_pv_actual_power": fetch_pv_actual_profile(48),
        "profile_48h_house_actual_power": fetch_house_actual_profile(48, data["samples"]),
        "profile_48h_grid_net_actual_power": fetch_net_actual_profile(48),
        "timestamp": now.isoformat(),
    }

    shadow_comparison = forecast_diagnostics.get("shadow_comparison")
    data["last_output"] = out
    save_state(data)
    if shadow_comparison:
        out["_shadow_forecast_comparison"] = shadow_comparison
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            data = load_state()
            out = fallback_output("error", str(exc), data, now_iso)
            print(json.dumps(out, ensure_ascii=False))
        except Exception:
            print(json.dumps({"mode": "error", "reason": str(exc)}))

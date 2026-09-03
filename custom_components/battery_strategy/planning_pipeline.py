#!/usr/bin/env python3
"""Home Assistant-facing orchestration for one planning pipeline run."""

import bisect
import datetime as dt
import json
import math
import os
import time
from functools import lru_cache
from zoneinfo import ZoneInfo

from .contracts import (
    ForecastRequest,
    LoadForecastContext,
    SlotKey,
)
from .forecasting import (
    FeatureStoreForecastNotReady,
    ForecastModelConfig,
    ForecastTargetInput,
    build_feature_store_forecast,
    feature_store_forecast_readiness,
)
from .market_context import MarketContextConfig, MarketContextService
from .optimizer_state import load_state_document, save_state_document
from .planning_service import PlanningService, PlanningSettings
from .savings import SavingsConfig, SavingsEntities, SavingsLedger

STATE_FILE = "/config/battery_strategy_optimizer_state.json"
_RUNTIME_STATES = {}
_RUNTIME_HISTORY_SERIES = {}
_RUNTIME_PRICE_INTERVALS = []
_RUNTIME_FORECAST_HISTORY = ()
_RUNTIME_FORECAST_WEATHER = ()
_RUNTIME_FORECAST_CONTEXT = None
_RUNTIME_FORECAST_COMPONENT_SPECS = ()

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
ETA_C = ETA_RT**0.5
ETA_D = ETA_RT**0.5
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
ECONOMIC_COST_TIE_EUR = 1e-9
MIN_MARGIN_CT = 2.0
HISTORY_DAYS = 60
ACTUAL_SAVINGS_DAYS = 21
BIAS_ALPHA = 0.12
SLOT_BIAS_ALPHA = 0.08
SLOTS_PER_DAY = 96
TRACE_MIN_INTERVAL_S = 240
TRACE_RETENTION_DAYS = 14
TRACE_MAX_POINTS = 8000
EEX_CACHE_TTL_S = 6 * 3600
OPEN_METEO_TZ = ZoneInfo("Europe/Berlin")
TERMINAL_RANK_THRESHOLD = 0.35
TERMINAL_VALUE_CAP_CT = 25.0
MICROCYCLE_LOOKBACK_SLOTS = 8
CHARGE_DEFERRAL_MARGIN_CT = 0.5
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
PV_CAPACITY_KWP = 1.0
PV_INVERTER_KW = 1.0

# PV surplus anti-cycling thresholds
PV_SURPLUS_START_AVG_W = 50.0
PV_SURPLUS_MIN_SAMPLE_W = 40.0
PV_SURPLUS_REQUIRED_COUNT = 1
PV_SURPLUS_WINDOW_SAMPLES = 1

def _configure(context):
    """Apply one config-entry runtime context before an optimizer run."""
    global STATE_FILE
    global _RUNTIME_STATES, _RUNTIME_HISTORY_SERIES, _RUNTIME_PRICE_INTERVALS
    global _RUNTIME_FORECAST_HISTORY, _RUNTIME_FORECAST_WEATHER
    global _RUNTIME_FORECAST_CONTEXT, _RUNTIME_FORECAST_COMPONENT_SPECS
    global CAP_KWH, SOC_MIN, SOC_MAX, MIN_E_KWH, MAX_E_KWH, MAX_P_W, MAX_E_SLOT_KWH
    global \
        MAX_CHARGE_P_W, \
        MAX_DISCHARGE_P_W, \
        MAX_CHARGE_E_SLOT_KWH, \
        MAX_DISCHARGE_E_SLOT_KWH
    global \
        PV_CHARGING_ENABLED, \
        GRID_CHARGING_ENABLED, \
        DISCHARGE_ENABLED, \
        PLANNING_HORIZON_H
    global ETA_RT, ETA_C, ETA_D, MIN_MARGIN_CT, PV_EXPORT_OPPORTUNITY_CT
    global OPEN_METEO_TZ, PV_CAPACITY_KWP, PV_INVERTER_KW

    config_dir = str(context.get("config_dir") or "/config")
    STATE_FILE = os.path.join(config_dir, "battery_strategy_optimizer_state.json")
    _RUNTIME_STATES = dict(context.get("states") or {})
    _RUNTIME_HISTORY_SERIES = {
        key: tuple(values)
        for key, values in (context.get("history_series") or {}).items()
    }
    _RUNTIME_PRICE_INTERVALS = list(context.get("price_intervals") or [])
    _RUNTIME_FORECAST_HISTORY = tuple(context.get("forecast_history") or ())
    _RUNTIME_FORECAST_WEATHER = tuple(context.get("forecast_weather") or ())
    _RUNTIME_FORECAST_CONTEXT = context.get("forecast_context")
    _RUNTIME_FORECAST_COMPONENT_SPECS = tuple(
        context.get("forecast_component_specs") or ()
    )

    CAP_KWH = max(0.5, float(context.get("battery_capacity_kwh") or 6.0))
    SOC_MIN = max(0.0, min(100.0, float(context.get("min_soc_pct") or 0.0)))
    SOC_MAX = max(SOC_MIN, min(100.0, float(context.get("max_soc_pct") or 100.0)))
    MIN_E_KWH = CAP_KWH * SOC_MIN / 100.0
    MAX_E_KWH = CAP_KWH * SOC_MAX / 100.0
    MAX_CHARGE_P_W = max(
        0.0,
        float(
            context.get("max_charge_power_w") or context.get("max_power_w") or 2400.0
        ),
    )
    MAX_DISCHARGE_P_W = max(
        0.0,
        float(
            context.get("max_discharge_power_w") or context.get("max_power_w") or 2400.0
        ),
    )
    MAX_P_W = max(MAX_CHARGE_P_W, MAX_DISCHARGE_P_W)
    MAX_E_SLOT_KWH = (MAX_P_W / 1000.0) * SLOT_H
    MAX_CHARGE_E_SLOT_KWH = (MAX_CHARGE_P_W / 1000.0) * SLOT_H
    MAX_DISCHARGE_E_SLOT_KWH = (MAX_DISCHARGE_P_W / 1000.0) * SLOT_H
    PV_CHARGING_ENABLED = str(context.get("pv_charging") or "on") != "off"
    GRID_CHARGING_ENABLED = str(context.get("grid_charging") or "off") != "off"
    DISCHARGE_ENABLED = str(context.get("discharge") or "load") != "off"
    PLANNING_HORIZON_H = max(1, min(48, int(context.get("planning_horizon_h") or 48)))
    ETA_RT = max(0.01, min(1.0, float(context.get("round_trip_efficiency") or 0.8)))
    ETA_C = ETA_RT**0.5
    ETA_D = ETA_RT**0.5
    MIN_MARGIN_CT = max(0.0, float(context.get("min_margin_ct_per_kwh", 2.0)))
    PV_EXPORT_OPPORTUNITY_CT = max(
        0.0, float(context.get("feed_in_tariff_ct_per_kwh", 0.0))
    )

    timezone = str(context.get("timezone") or "UTC")
    OPEN_METEO_TZ = ZoneInfo(timezone)
    PV_CAPACITY_KWP = max(0.1, float(context.get("pv_capacity_kwp") or 1.0))
    PV_INVERTER_KW = max(
        0.1, float(context.get("pv_inverter_power_kw") or PV_CAPACITY_KWP)
    )
    # This cache depends on mutable runtime timezone configuration.
    local_dt_from_ts.cache_clear()


def _market_context_service() -> MarketContextService:
    """Build the setup-neutral market boundary from current runtime options."""
    return MarketContextService(
        MarketContextConfig(
            timezone=OPEN_METEO_TZ,
            round_trip_efficiency=ETA_RT,
            min_margin_ct_per_kwh=MIN_MARGIN_CT,
            terminal_rank_threshold=TERMINAL_RANK_THRESHOLD,
            terminal_value_cap_ct=TERMINAL_VALUE_CAP_CT,
            slots_per_day=SLOTS_PER_DAY,
            eex_cache_ttl_s=EEX_CACHE_TTL_S,
            proxy_min_full_day_slots=EEX_PROXY_MIN_FULL_DAY_SLOTS,
            proxy_recent_days=EEX_PROXY_RECENT_DAYS,
            proxy_min_retail_markup_ct=EEX_PROXY_MIN_RETAIL_MARKUP_CT,
            proxy_max_base_retail_markup_ct=EEX_PROXY_MAX_BASE_RETAIL_MARKUP_CT,
            proxy_max_peak_retail_markup_ct=EEX_PROXY_MAX_PEAK_RETAIL_MARKUP_CT,
            proxy_min_price_ct=EEX_PROXY_MIN_PRICE_CT,
            proxy_max_price_ct=EEX_PROXY_MAX_PRICE_CT,
        )
    )


def _planning_service() -> PlanningService:
    return PlanningService(
        market_context=_market_context_service(),
        settings=PlanningSettings(
            battery_capacity_kwh=CAP_KWH,
            min_soc_pct=SOC_MIN,
            max_soc_pct=SOC_MAX,
            max_charge_power_w=MAX_CHARGE_P_W,
            max_discharge_power_w=MAX_DISCHARGE_P_W,
            round_trip_efficiency=ETA_RT,
            min_margin_ct_per_kwh=MIN_MARGIN_CT,
            export_opportunity_ct_per_kwh=PV_EXPORT_OPPORTUNITY_CT,
            pv_charging_allowed=PV_CHARGING_ENABLED,
            grid_charging_allowed=GRID_CHARGING_ENABLED,
            discharge_allowed=DISCHARGE_ENABLED,
            pv_recovery_confidence=PV_RECOVERY_CONFIDENCE,
            pv_recovery_reserve_kwh=PV_RECOVERY_RESERVE_KWH,
            slot_hours=SLOT_H,
        ),
    )


def _update_actual_savings(data, now_ts):
    """Update measured savings through its independent accounting boundary."""
    return SavingsLedger(
        config=SavingsConfig(
            timezone=OPEN_METEO_TZ,
            retention_days=HISTORY_DAYS,
            entities=SavingsEntities(
                price=E_PRICE_EUR,
                battery_input_energy=E_BATTERY_INPUT_ENERGY,
                battery_output_energy=E_BATTERY_OUTPUT_ENERGY,
                grid_import=E_GRID_IMPORT,
                grid_export=E_GRID_EXPORT,
                battery_power=E_BATTERY_POWER,
            ),
        ),
        history_reader=fetch_sensor_series_many,
        price_reader=read_tibber_intervals_for_dates,
    ).update(data, now_ts)


def get_latest_states(entity_ids):
    """Return only the immutable live-state snapshot captured by the adapter."""
    return {entity_id: _RUNTIME_STATES.get(entity_id) for entity_id in entity_ids}


def fetch_sensor_series_many(entity_ids, cutoff_ts):
    """Return bounded Recorder history captured through the HA adapter."""
    cutoff = float(cutoff_ts)
    return {
        entity_id: [
            (float(timestamp), float(value))
            for timestamp, value in _RUNTIME_HISTORY_SERIES.get(entity_id, ())
            if float(timestamp) >= cutoff
        ]
        for entity_id in entity_ids
    }


def fetch_sensor_series(entity_id, cutoff_ts):
    """Return one normalized series from the captured history snapshot."""
    return fetch_sensor_series_many([entity_id], cutoff_ts)[entity_id]


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
    except Exception:
        return default


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


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
    return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).astimezone(
        OPEN_METEO_TZ
    )


def bootstrap_samples_from_features(now_ts, days=21):
    """Build calibration samples from canonical finalized features."""
    cutoff_ms = int((float(now_ts) - days * 86400) * 1000)
    samples = []
    for feature in _RUNTIME_FORECAST_HISTORY:
        if feature.slot.start_ms < cutoff_ms:
            continue
        factor_w = 1000.0 / SLOT_H
        battery_power_w = (
            feature.battery_discharge_kwh - feature.battery_charge_kwh
        ) * factor_w
        ev_w = feature.ev_charge_kwh * factor_w
        house_w = feature.house_load_no_ev_kwh * factor_w
        samples.append(
            {
                "ts": feature.slot.start_ms / 1000.0,
                "load_w": house_w,
                "house_w": house_w,
                "house_total_w": house_w + ev_w,
                "wallbox_w": ev_w,
                "grid_import_w": feature.grid_import_kwh * factor_w,
                "grid_export_w": feature.grid_export_kwh * factor_w,
                "pv_w": feature.pv_generation_kwh * factor_w,
                "bat_in_out_w": battery_power_w,
                "hp_w": 0.0,
                "price_ct": float(feature.price_ct_per_kwh or 0.0),
                "soc": -1,
            }
        )
    return samples[-12000:]


def avg_power(samples, start_ts, end_ts, key):
    vals = [s.get(key, 0.0) for s in samples if start_ts <= s.get("ts", 0) <= end_ts]
    return (sum(vals) / len(vals)) if vals else None


def recent_surplus_stable(samples):
    recent = samples[-PV_SURPLUS_WINDOW_SAMPLES:]
    if len(recent) < PV_SURPLUS_WINDOW_SAMPLES:
        return False, 0.0
    surplus_vals = [
        float(s.get("pv_w", 0.0)) - float(s.get("load_w", 0.0)) for s in recent
    ]
    avg_surplus = sum(surplus_vals) / len(surplus_vals)
    high_count = sum(1 for x in surplus_vals if x > PV_SURPLUS_MIN_SAMPLE_W)
    stable = (avg_surplus > PV_SURPLUS_START_AVG_W) and (
        high_count >= PV_SURPLUS_REQUIRED_COUNT
    )
    return stable, avg_surplus


def weather_factor_from_cloud_rad(cloud_cover, shortwave_radiation):
    cloud_factor = clamp(1.0 - float(cloud_cover or 0.0) / 130.0, 0.35, 1.05)
    rad_factor = clamp(float(shortwave_radiation or 0.0) / 650.0, 0.2, 1.1)
    return 0.6 * cloud_factor + 0.4 * rad_factor


def weather_snapshot(now_ts_ms):
    """Project the canonical slot weather into the published runtime shape."""
    slots = tuple(_RUNTIME_FORECAST_WEATHER)
    if not slots:
        return None
    current = next(
        (item for item in slots if item.slot.start_ms <= now_ts_ms < item.slot.end_ms),
        slots[0],
    )
    hourly = {}
    for item in slots:
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, dt.timezone.utc
        ).astimezone(OPEN_METEO_TZ)
        key = local.replace(minute=0, second=0, microsecond=0).isoformat()
        cloud = item.cloud_cover_pct
        radiation = item.shortwave_radiation_w_m2
        hourly[key] = {
            "cloud_cover": cloud,
            "shortwave_radiation": radiation,
            "weather_factor": round(weather_factor_from_cloud_rad(cloud, radiation), 4),
        }
    return {
        "cloud_cover": current.cloud_cover_pct,
        "shortwave_radiation": current.shortwave_radiation_w_m2,
        "weather_factor": round(
            weather_factor_from_cloud_rad(
                current.cloud_cover_pct, current.shortwave_radiation_w_m2
            ),
            4,
        ),
        "hourly": hourly,
    }


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
    energy = clamp(
        float(data.get("virtual_energy_kwh", CAP_KWH * 0.5)), MIN_E_KWH, MAX_E_KWH
    )
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
        "power_w": round(
            float(power_w if not mode.startswith("discharge_") else -power_w), 1
        ),
        "charge_fc_w": round(charge_w, 1),
        "discharge_fc_w": round(discharge_w, 1),
    }
    if (
        trace
        and int(ts_ms) - int(trace[-1].get("ts_ms", 0)) < TRACE_MIN_INTERVAL_S * 1000
    ):
        trace[-1] = point
    else:
        trace.append(point)
    cutoff_ms = int(
        (dt.datetime.now(dt.timezone.utc).timestamp() - TRACE_RETENTION_DAYS * 86400)
        * 1000
    )
    data["virtual_trace"] = compact_virtual_trace(
        [x for x in trace if x.get("ts_ms", 0) >= cutoff_ms]
    )[-TRACE_MAX_POINTS:]


def build_price_profile(intervals, date_str):
    arr = [it for it in intervals if it["dt"].date().isoformat() == date_str]
    return [
        [int(it["dt"].timestamp() * 1000), round(it["price_eur"] * 100.0, 3)]
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
        local_date_set_between(
            dt.datetime.now(dt.timezone.utc).timestamp(),
            dt.datetime.now(dt.timezone.utc).timestamp(),
        )
    )
    p_now_eur = tibber_price_eur_at(
        dt.datetime.now(dt.timezone.utc).timestamp(), price_ts, price_vals
    )
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
    weather = weather_snapshot(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000))
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
                    parsed = parsed.replace(tzinfo=OPEN_METEO_TZ)
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


def local_date_set_between(start_ts, end_ts):
    start_day = (
        dt.datetime.fromtimestamp(float(start_ts), dt.timezone.utc)
        .astimezone(OPEN_METEO_TZ)
        .date()
    )
    end_day = (
        dt.datetime.fromtimestamp(float(end_ts), dt.timezone.utc)
        .astimezone(OPEN_METEO_TZ)
        .date()
    )
    days = set()
    cur = start_day
    while cur <= end_day:
        days.add(cur.isoformat())
        cur += dt.timedelta(days=1)
    return days


def build_tibber_price_index(date_set):
    intervals = read_tibber_intervals_for_dates(date_set)
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


def build_production_forecast(
    intervals,
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
    pv_capacity_kwp,
    pv_inverter_kw,
    history=None,
    context=None,
    weather=None,
    component_specs=None,
):
    """Build the sole production forecast from finalized feature history."""
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
    immutable_history = tuple(_RUNTIME_FORECAST_HISTORY if history is None else history)
    forecast_context = context or _RUNTIME_FORECAST_CONTEXT
    if not isinstance(forecast_context, LoadForecastContext):
        raise FeatureStoreForecastNotReady("missing_current_load_context")
    immutable_weather = tuple(_RUNTIME_FORECAST_WEATHER if weather is None else weather)
    immutable_component_specs = tuple(
        _RUNTIME_FORECAST_COMPONENT_SPECS
        if component_specs is None
        else component_specs
    )
    config = ForecastModelConfig(
        timezone=request.timezone,
        load_bias=float(load_bias),
        load_slot_biases=tuple(float(value) for value in load_bias_slots),
        pv_global_bias=float(pv_global_bias),
        pv_slot_biases=tuple(float(value) for value in pv_bias_slots),
        current_weather_factor=float(weather_factor),
        current_pv_w=(
            None if pv_now_actual_w is None else max(0.0, float(pv_now_actual_w))
        ),
        tomorrow_date=(intervals[0]["dt"].date() + dt.timedelta(days=1)).isoformat(),
        tomorrow_energy_kwh=(
            None if forecast_tomorrow_kwh is None else float(forecast_tomorrow_kwh)
        ),
        pv_capacity_kwp=float(pv_capacity_kwp),
        pv_inverter_kw=float(pv_inverter_kw),
    )
    readiness = feature_store_forecast_readiness(
        tuple(
            item for item in immutable_history if item.slot.end_ms <= request.as_of_ms
        ),
        component_specs=immutable_component_specs,
    )
    forecast = build_feature_store_forecast(
        request,
        immutable_history,
        tuple(targets),
        forecast_context,
        config,
        weather=immutable_weather,
        component_specs=immutable_component_specs,
    )
    load_w = [slot.energy.p50_kwh / SLOT_H * 1000.0 for slot in forecast.load.slots]
    pv_w = [slot.energy.p50_kwh / SLOT_H * 1000.0 for slot in forecast.pv.slots]
    if len(load_w) != len(intervals) or len(pv_w) != len(intervals):
        raise ValueError("production forecast does not match requested slot grid")
    diagnostics = {
        "source": "feature_store",
        "slot_count": len(request.slots),
        "runtime_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "model_version": f"{forecast.load.model_version}+{forecast.pv.model_version}",
        "history_slot_count": readiness.history_slots,
        "load_usable_slots": readiness.load_usable_slots,
        "pv_usable_slots": readiness.pv_usable_slots,
        "component_usable_slots": readiness.component_usable_slots,
        "history_span_days": readiness.history_span_days,
    }
    return forecast, diagnostics


def build_forecast_targets(intervals, weather_factor, weather_hourly=None):
    """Build the weather-aligned target grid at the composition boundary."""
    targets = []
    for item in intervals:
        hour_key = item["dt"].replace(minute=0, second=0, microsecond=0).isoformat()
        slot_weather_factor = float(
            (weather_hourly or {})
            .get(hour_key, {})
            .get("weather_factor", weather_factor)
        )
        targets.append(ForecastTargetInput(item["dt"], slot_weather_factor))
    return tuple(targets)


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
    plan_power = int(round(abs(float(first_plan.get("power_w", 0.0)))))

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


def run(runtime_context=None):
    """Execute one planning refresh from an explicitly captured snapshot."""
    if runtime_context is not None:
        _configure(runtime_context)
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
        return out

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
        soc = (
            persisted_soc
            if persisted_soc is not None and 0.0 <= persisted_soc <= 100.0
            else None
        )
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
        data["samples"] = bootstrap_samples_from_features(now_ts, days=21)

    data["pv_bias_slots"] = normalize_slot_biases(data.get("pv_bias_slots"), 0.5, 1.6)
    data["load_bias_slots"] = normalize_slot_biases(
        data.get("load_bias_slots"), 0.6, 1.6
    )

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
    ) = _update_actual_savings(data, now_ts)

    # actual_daily_savings is maintained inside the savings ledger
    # via data["actual_daily_savings"] directly; retrieve for output helpers.
    actual_daily_savings = data["actual_daily_savings"]
    actual_inventory_deliverable_kwh = None
    actual_inventory_cost_ct_per_kwh = None
    actual_today_stats = actual_daily_savings.get(today, {})

    load_bias = clamp(float(data.get("load_bias", 1.0)), 0.6, 1.6)
    weather_factor = weather_factor_from_cloud_rad(cloud, rad)
    pv_bias = clamp(float(data.get("pv_bias", 1.0)), 0.5, 1.4)
    pv_surplus_w = real_charge_follow_surplus_w(
        grid_import_w, grid_export_w, bat_in_out_w
    )
    # Net import that would exist without battery and EV influence.
    net_no_battery_no_ev_now_w = net_no_battery_no_ev_w(
        grid_import_w, grid_export_w, bat_in_out_w, wallbox_w
    )
    net_no_battery_with_ev_now_w = net_no_battery_with_ev_w(
        grid_import_w, grid_export_w, bat_in_out_w
    )
    pv_surplus_stable, pv_surplus_avg = recent_surplus_stable(data["samples"])
    rte_break_even_ct = (p_now / ETA_RT) + MIN_MARGIN_CT if p_now is not None else None
    expected_spread_ct = (p_future_max * ETA_RT) - p_now if p_now is not None else None

    mode = "idle"
    rec_w = 0
    reason = "15min Tibber plan"

    due = [p for p in data["predictions"] if p.get("target_ts", 0) <= now_ts]
    data["predictions"] = [
        p for p in data["predictions"] if p.get("target_ts", 0) > now_ts
    ][-1200:]

    for pred in due:
        end_ts = pred["target_ts"]
        start_ts = end_ts - 3600
        end_local = dt.datetime.fromtimestamp(end_ts, tz=local_now.tzinfo)
        slot = slot_index_for_dt(end_local)
        pv_avg = avg_power(data["samples"], start_ts, end_ts, "pv_w")
        load_avg = avg_power(data["samples"], start_ts, end_ts, "load_w")
        price_target = avg_power(
            data["samples"], end_ts - 900, end_ts + 900, "price_ct"
        )
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
            data["pv_bias"] = clamp(
                (1.0 - BIAS_ALPHA) * float(data.get("pv_bias", 1.0))
                + BIAS_ALPHA * pv_ratio,
                0.5,
                1.6,
            )
            old = data["pv_bias_slots"][slot]
            data["pv_bias_slots"][slot] = clamp(
                (1.0 - SLOT_BIAS_ALPHA) * old + SLOT_BIAS_ALPHA * pv_ratio, 0.5, 1.6
            )

        load_pred = max(0.2, float(pred.get("load_pred_kwh", 0.0)))
        load_ratio = clamp(load_actual / load_pred, 0.75, 1.25)
        data["load_bias"] = clamp(
            (1.0 - BIAS_ALPHA) * float(data.get("load_bias", 1.0))
            + BIAS_ALPHA * load_ratio,
            0.6,
            1.6,
        )
        old_l = data["load_bias_slots"][slot]
        data["load_bias_slots"][slot] = clamp(
            (1.0 - SLOT_BIAS_ALPHA) * old_l + SLOT_BIAS_ALPHA * load_ratio, 0.6, 1.6
        )

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
    data["backtests"] = [b for b in data["backtests"] if b.get("ts", 0) >= bt_cutoff][
        -8000:
    ]

    bt24 = [b for b in data["backtests"] if b.get("ts", 0) >= now_ts - 86400]
    bt7d = [b for b in data["backtests"] if b.get("ts", 0) >= now_ts - 7 * 86400]

    def mae(items, key):
        return (sum(i.get(key, 0.0) for i in items) / len(items)) if items else None

    hit24 = (
        (100.0 * sum(1 for x in bt24 if x.get("success")) / len(bt24)) if bt24 else None
    )

    market_context = _market_context_service()
    eex_days = market_context.get_eex_day_context(data, local_now)
    intervals_all = read_tibber_intervals_for_dates({today, tomorrow})
    intervals_all, tomorrow_price_source = market_context.apply_eex_proxy_prices(
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
    else:
        start_e = advance_virtual_energy(data, now_ts)

    load_bias_plan = clamp(float(data.get("load_bias", load_bias)), 0.6, 1.6)
    inventory_accounting_floor_ct = None
    if actual_inventory_cost_ct_per_kwh is None:
        actual_today_charge_in_kwh = float(
            actual_today_stats.get("charge_grid_kwh", 0.0)
        ) + float(actual_today_stats.get("charge_pv_kwh", 0.0))
        actual_today_charge_cost_eur = float(
            actual_today_stats.get("charge_cost_eur", 0.0)
        )
        if actual_today_charge_in_kwh > 0.25:
            actual_inventory_cost_ct_per_kwh = (
                actual_today_charge_cost_eur
                / max(1e-9, actual_today_charge_in_kwh * ETA_RT)
            ) * 100.0
    if actual_inventory_cost_ct_per_kwh is not None:
        inventory_accounting_floor_ct = actual_inventory_cost_ct_per_kwh + MIN_MARGIN_CT
    forecast_targets = build_forecast_targets(
        intervals,
        weather_factor,
        (inputs.get("weather") or {}).get("hourly"),
    )
    forecast_bundle, forecast_diagnostics = build_production_forecast(
        intervals,
        forecast_targets,
        now_local=local_now,
        weather_factor=weather_factor,
        forecast_tomorrow_kwh=pv_tomorrow_kwh,
        load_bias=load_bias_plan,
        load_bias_slots=data["load_bias_slots"],
        pv_bias_slots=data["pv_bias_slots"],
        pv_now_actual_w=max(0.0, pv_w),
        pv_global_bias=pv_bias,
        pv_capacity_kwp=PV_CAPACITY_KWP,
        pv_inverter_kw=PV_INVERTER_KW,
    )
    plan = _planning_service().plan(
        intervals=intervals,
        samples=data["samples"],
        start_energy_kwh=start_e,
        eex_days=eex_days,
        forecast_bundle=forecast_bundle,
        forecast_diagnostics=forecast_diagnostics,
    )
    forecast_diagnostics = plan.get("forecast_diagnostics", {})
    future_points = plan["points"]
    next_hour_points = future_points[:4]
    load_fc_kwh = (
        sum(max(0.0, float(point.get("load_fc_w", 0.0))) for point in next_hour_points)
        / 1000.0
        * SLOT_H
    )
    pv_corr_kwh = (
        sum(max(0.0, float(point.get("pv_fc_w", 0.0))) for point in next_hour_points)
        / 1000.0
        * SLOT_H
    )
    net_kwh = max(0.0, load_fc_kwh - pv_corr_kwh)
    planned_mode = "idle"
    planned_power_w = 0
    if future_points:
        planned_mode, planned_power_w = derive_planned_dispatch(future_points[0])
    mode = planned_mode
    rec_w = planned_power_w
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
        append_virtual_trace(
            data, int(now_ts * 1000), today, (start_e / CAP_KWH) * 100.0, mode, rec_w
        )
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
    price_obs = market_context.compute_price_quantiles(
        data["samples"], local_now, p_now, profile_tomorrow["price"]
    )
    for k in (
        "pv_fc_power",
        "grid_import_fc_power",
        "grid_export_fc_power",
        "grid_net_fc_power",
    ):
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
        "planned_charge_power_w": int(
            planned_power_w
            if planned_mode in ("charge_grid", "charge_pv_surplus", "charge_follow")
            else 0
        ),
        "planned_discharge_power_w": int(
            planned_power_w if planned_mode.startswith("discharge_") else 0
        ),
        "recommended_charge_power_w": int(
            rec_w
            if mode in ("charge_grid", "charge_pv_surplus", "charge_follow")
            else 0
        ),
        "recommended_discharge_power_w": int(
            rec_w if mode.startswith("discharge_") else 0
        ),
        "reason": reason,
        "expected_spread_ct": round(expected_spread_ct, 2)
        if expected_spread_ct is not None
        else None,
        "rte_break_even_ct": round(rte_break_even_ct, 2)
        if rte_break_even_ct is not None
        else None,
        "load_forecast_next_1h_kwh": round(load_fc_kwh, 3),
        "pv_forecast_raw_next_1h_kwh": round(pv_raw_kwh, 3),
        "pv_forecast_corrected_next_1h_kwh": round(pv_corr_kwh, 3),
        "net_load_forecast_next_1h_kwh": round(net_kwh, 3),
        "grid_import_forecast_next_1h_kwh": round(max(0.0, net_kwh), 3),
        "grid_export_forecast_next_1h_kwh": round(
            max(0.0, pv_corr_kwh - load_fc_kwh), 3
        ),
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
        "optimizer_source": plan.get("optimizer_source", "unknown"),
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
        "eex_trade_date_today": eex_today.get("base", {}).get("trade_date")
        or eex_today.get("peak", {}).get("trade_date"),
        "eex_base_tomorrow_ct": eex_tomorrow.get("base", {}).get("settl_ct_kwh"),
        "eex_peak_tomorrow_ct": eex_tomorrow.get("peak", {}).get("settl_ct_kwh"),
        "eex_spread_tomorrow_ct": eex_tomorrow.get("spread_ct_kwh"),
        "eex_trade_date_tomorrow": eex_tomorrow.get("base", {}).get("trade_date")
        or eex_tomorrow.get("peak", {}).get("trade_date"),
        "eex_base_day_after_ct": eex_day_after.get("base", {}).get("settl_ct_kwh"),
        "eex_peak_day_after_ct": eex_day_after.get("peak", {}).get("settl_ct_kwh"),
        "eex_spread_day_after_ct": eex_day_after.get("spread_ct_kwh"),
        "eex_trade_date_day_after": eex_day_after.get("base", {}).get("trade_date")
        or eex_day_after.get("peak", {}).get("trade_date"),
        "baseline_cost_today_eur": plan.get("daily_costs", {})
        .get(today, {})
        .get("base_eur"),
        "optimized_cost_today_eur": plan.get("daily_costs", {})
        .get(today, {})
        .get("with_bat_eur"),
        "baseline_cost_tomorrow_eur": plan.get("daily_costs", {})
        .get(tomorrow, {})
        .get("base_eur"),
        "optimized_cost_tomorrow_eur": plan.get("daily_costs", {})
        .get(tomorrow, {})
        .get("with_bat_eur"),
        "estimated_savings_today_eur": round(save_today, 3),
        "estimated_savings_tomorrow_eur": round(save_tom, 3),
        "estimated_savings_cumulative_eur": round(cumulative, 3),
        "actual_savings_today_eur": round(actual_today_saving, 3),
        "actual_savings_cumulative_eur": actual_savings_lifetime_eur,
        "actual_savings_lifetime_eur": actual_savings_lifetime_eur,
        "actual_inventory_deliverable_kwh": actual_inventory_deliverable_kwh,
        "actual_inventory_cost_ct_per_kwh": actual_inventory_cost_ct_per_kwh,
        "inventory_accounting_floor_ct": round(inventory_accounting_floor_ct, 3)
        if inventory_accounting_floor_ct is not None
        else None,
        "actual_battery_charge_grid_today_kwh": float(
            actual_daily_savings.get(today, {}).get("charge_grid_kwh", 0.0)
        ),
        "actual_battery_charge_pv_today_kwh": float(
            actual_daily_savings.get(today, {}).get("charge_pv_kwh", 0.0)
        ),
        "actual_battery_discharge_credited_today_kwh": float(
            actual_daily_savings.get(today, {}).get("discharge_used_kwh", 0.0)
        ),
        "actual_battery_charge_cost_today_eur": float(
            actual_daily_savings.get(today, {}).get("charge_cost_eur", 0.0)
        ),
        "actual_battery_discharge_credit_today_eur": float(
            actual_daily_savings.get(today, {}).get("discharge_credit_eur", 0.0)
        ),
        "profile_today_price": profile_today["price"],
        "profile_today_soc": profile_today["soc"],
        "profile_today_power": profile_today["power"],
        "profile_today_charge_power": profile_today["charge_power"],
        "profile_today_pv_charge_power": profile_today["pv_charge_power"],
        "profile_today_grid_charge_power": profile_today["grid_charge_power"],
        "profile_today_required_charge_power": profile_today["required_charge_power"],
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
        "profile_tomorrow_pv_charge_power": profile_tomorrow["pv_charge_power"],
        "profile_tomorrow_grid_charge_power": profile_tomorrow["grid_charge_power"],
        "profile_tomorrow_required_charge_power": profile_tomorrow[
            "required_charge_power"
        ],
        "profile_tomorrow_discharge_power": profile_tomorrow["discharge_power"],
        "profile_tomorrow_discharge_budget_kwh": profile_tomorrow[
            "discharge_budget_kwh"
        ],
        "profile_tomorrow_pv_fc_power": profile_tomorrow["pv_fc_power"],
        "profile_tomorrow_grid_import_fc_power": profile_tomorrow[
            "grid_import_fc_power"
        ],
        "profile_tomorrow_grid_export_fc_power": profile_tomorrow[
            "grid_export_fc_power"
        ],
        "profile_tomorrow_grid_net_fc_power": profile_tomorrow["grid_net_fc_power"],
        "profile_48h_pv_fc_power": [[p["ts_ms"], p["pv_fc_w"]] for p in future_points],
        "profile_48h_house_fc_power": [
            [p["ts_ms"], p["load_fc_w"]] for p in future_points
        ],
        "profile_48h_charge_fc_power": [
            [p["ts_ms"], p["charge_fc_w"]] for p in future_points
        ],
        "profile_48h_pv_charge_fc_power": [
            [p["ts_ms"], p.get("pv_charge_fc_w", 0.0)] for p in future_points
        ],
        "profile_48h_grid_charge_fc_power": [
            [p["ts_ms"], p.get("grid_charge_fc_w", 0.0)] for p in future_points
        ],
        "profile_48h_required_charge_fc_power": [
            [p["ts_ms"], p.get("required_charge_fc_w", 0.0)] for p in future_points
        ],
        "profile_48h_discharge_fc_power": [
            [p["ts_ms"], p["discharge_fc_w"]] for p in future_points
        ],
        "profile_48h_discharge_budget_kwh": [
            [p["ts_ms"], p.get("discharge_budget_kwh", 0.0)] for p in future_points
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
        "profile_48h_house_actual_power": fetch_house_actual_profile(
            48, data["samples"]
        ),
        "profile_48h_grid_net_actual_power": fetch_net_actual_profile(48),
        "timestamp": now.isoformat(),
    }

    data["last_output"] = out
    save_state(data)
    return out

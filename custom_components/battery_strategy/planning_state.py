"""Persistence and simulation state for the planning application."""

from __future__ import annotations

import datetime as dt

from .optimizer_state import load_state_document, save_state_document
from .runtime_measurements import migrate_state_sample_v9, normalize_samples

SLOTS_PER_DAY = 96
TRACE_MIN_INTERVAL_S = 240
TRACE_RETENTION_DAYS = 14
TRACE_MAX_POINTS = 8000


def _clamp(value, lower, upper):
    return max(lower, min(upper, value))


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


def normalize_slot_biases(arr, lo, hi):
    if not isinstance(arr, list) or len(arr) != SLOTS_PER_DAY:
        return [1.0] * SLOTS_PER_DAY
    out = []
    for v in arr:
        try:
            out.append(_clamp(float(v), lo, hi))
        except (TypeError, ValueError):
            out.append(1.0)
    return out


def load_state(runtime):
    settings = runtime.settings
    default_state = {
        "samples": [],
        "predictions": [],
        "backtests": [],
        "pv_bias": 1.0,
        "load_bias": 1.0,
        "pv_bias_slots": [1.0] * SLOTS_PER_DAY,
        "load_bias_slots": [1.0] * SLOTS_PER_DAY,
        "virtual_energy_kwh": settings.battery_capacity_kwh * 0.5,
        "virtual_last_ts": None,
        "virtual_last_mode": "idle",
        "virtual_last_power_w": 0.0,
        "virtual_trace": [],
        "last_known_soc_pct": None,
        "eex_cache": {},
        "daily_savings": {},
        "actual_daily_savings": {},
        "last_output": {},
        "state_schema": 9,
    }
    try:
        data = load_state_document(settings.state_file)
        if data is None:
            return default_state
        for k, v in default_state.items():
            data.setdefault(k, v)
        # Phase-1 comparison traces are obsolete once the extracted forecast is
        # the sole production path. Drop both historical names during migration.
        data.pop("forecast_shadow_trace", None)
        data.pop("forecast_parity_trace", None)
        if int(data.get("state_schema", 0)) < 4:
            data["virtual_energy_kwh"] = settings.battery_capacity_kwh * 0.5
            data["virtual_last_ts"] = None
            data["virtual_last_mode"] = "idle"
            data["virtual_last_power_w"] = 0.0
            data["virtual_trace"] = []
        if int(data.get("state_schema", 0)) < 9:
            data["samples"] = [
                migrate_state_sample_v9(sample)
                for sample in data.get("samples", [])
                if isinstance(sample, dict)
            ]
        else:
            data["samples"] = normalize_samples(data.get("samples", []))
        data["state_schema"] = 9
        data["virtual_trace"] = compact_virtual_trace(data.get("virtual_trace", []))
        return data
    except (OSError, TypeError, ValueError):
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


def save_state(runtime, data):
    save_state_document(runtime.settings.state_file, data)


def advance_virtual_energy(settings, data, now_ts):
    energy = _clamp(
        float(data.get("virtual_energy_kwh", settings.battery_capacity_kwh * 0.5)),
        settings.min_energy_kwh,
        settings.max_energy_kwh,
    )
    last_ts = data.get("virtual_last_ts")
    if not last_ts:
        data["virtual_energy_kwh"] = energy
        return energy

    elapsed_h = max(0.0, (now_ts - float(last_ts)) / 3600.0)
    last_mode = data.get("virtual_last_mode", "idle")
    last_power_w = max(
        0.0,
        min(settings.max_power_w, float(data.get("virtual_last_power_w", 0.0))),
    )
    e_cmd = (last_power_w / 1000.0) * elapsed_h
    if last_mode in ("charge_grid", "charge_pv_surplus"):
        energy += e_cmd * settings.charge_efficiency
    elif str(last_mode).startswith("discharge_"):
        energy -= e_cmd / settings.discharge_efficiency
    energy = _clamp(energy, settings.min_energy_kwh, settings.max_energy_kwh)
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

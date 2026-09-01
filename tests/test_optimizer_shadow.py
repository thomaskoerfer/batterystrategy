"""Isolation and retention tests for the Phase-5 optimizer shadow."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

from custom_components.battery_strategy import optimizer_engine
from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    CommercialPolicy,
    ForecastBundle,
    ForecastSlot,
    LoadForecast,
    PvForecast,
    QuantileEnergy,
    SlotKey,
)
from custom_components.battery_strategy.optimizer_shadow import (
    OPERATIONAL_ACTION_TOLERANCE_KWH,
    OPERATIONAL_BUDGET_TOLERANCE_KWH,
    RETENTION_S,
    append_shadow_record,
    evaluate_optimizer_shadow,
    safe_evaluate_optimizer_shadow,
)


def _scenario():
    start = dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
    prices = [10.0, 10.0, 40.0, 40.0]
    loads = [0.1, 0.1, 0.5, 0.5]
    slots = tuple(
        SlotKey(
            int((start + dt.timedelta(minutes=15 * index)).timestamp() * 1000),
            int((start + dt.timedelta(minutes=15 * (index + 1))).timestamp() * 1000),
        )
        for index in range(4)
    )
    forecast = ForecastBundle(
        LoadForecast(
            "load",
            slots[0].start_ms,
            slots[0].start_ms,
            "load-v1",
            tuple(
                ForecastSlot(slot, QuantileEnergy(load))
                for slot, load in zip(slots, loads)
            ),
        ),
        PvForecast(
            "pv",
            slots[0].start_ms,
            slots[0].start_ms,
            "pv-v1",
            tuple(ForecastSlot(slot, QuantileEnergy(0.0)) for slot in slots),
        ),
    )
    intervals = [
        {
            "dt": start + dt.timedelta(minutes=15 * index),
            "price_eur": price / 100.0,
        }
        for index, price in enumerate(prices)
    ]
    return start, intervals, forecast


def test_shadow_matches_authoritative_plan_without_replacing_it():
    start, intervals, forecast = _scenario()
    with patch.multiple(
        optimizer_engine,
        CAP_KWH=6.0,
        SOC_MIN=10.0,
        SOC_MAX=100.0,
        MIN_E_KWH=0.6,
        MAX_E_KWH=6.0,
        MAX_CHARGE_P_W=2400.0,
        MAX_DISCHARGE_P_W=2400.0,
        MAX_CHARGE_E_SLOT_KWH=0.6,
        MAX_DISCHARGE_E_SLOT_KWH=0.6,
        ETA_RT=0.8,
        ETA_C=0.8**0.5,
        ETA_D=0.8**0.5,
        MIN_MARGIN_CT=2.0,
        PV_CHARGING_ENABLED=True,
        GRID_CHARGING_ENABLED=True,
        DISCHARGE_ENABLED=True,
        PV_EXPORT_OPPORTUNITY_CT=0.0,
    ):
        authoritative = optimizer_engine.build_virtual_plan(
            intervals, [], 0.6, forecast_bundle=forecast
        )
    snapshot = json.dumps(authoritative, sort_keys=True)
    result = evaluate_optimizer_shadow(
        intervals=intervals,
        forecast=forecast,
        start_energy_kwh=0.6,
        legacy_plan=authoritative,
        constraints=BatteryConstraints(6.0, 10.0, 100.0, 2400.0, 2400.0, 0.8),
        policy=CommercialPolicy(2.0),
        evaluated_at_ms=int(start.timestamp() * 1000),
    )
    assert result["status"] == "match"
    assert result["mismatch_slots"] == 0
    assert result["exact_status"] == "match"
    assert json.dumps(authoritative, sort_keys=True) == snapshot


def test_shadow_failure_is_contained():
    result = safe_evaluate_optimizer_shadow(evaluated_at_ms=123)
    assert result["status"] == "error"
    assert result["ts_ms"] == 123


def test_shadow_trace_has_time_and_count_retention(tmp_path):
    path = tmp_path / "shadow.jsonl"
    now_ms = 2_000_000_000_000
    old = {"ts_ms": now_ms - RETENTION_S * 1000 - 1, "status": "old"}
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    append_shadow_record(path, {"ts_ms": now_ms, "status": "match"}, now_ms=now_ms)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["ts_ms"] == now_ms
    assert records[0]["status"] == "match"


def test_retained_exact_mismatch_is_reclassified_without_losing_deltas(tmp_path):
    path = tmp_path / "shadow.jsonl"
    now_ms = 2_000_000_000_000
    old = {
        "ts_ms": now_ms - 1,
        "status": "mismatch",
        "mismatch_slots": 8,
        "max_charge_delta_kwh": OPERATIONAL_ACTION_TOLERANCE_KWH / 2,
        "max_discharge_delta_kwh": OPERATIONAL_ACTION_TOLERANCE_KWH / 2,
        "max_budget_delta_kwh": OPERATIONAL_BUDGET_TOLERANCE_KWH / 2,
        "max_soc_delta_pct": 0.02,
    }
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")

    append_shadow_record(path, {"ts_ms": now_ms, "status": "match"}, now_ms=now_ms)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["status"] == "match"
    assert records[0]["mismatch_slots"] == 0
    assert records[0]["exact_status"] == "mismatch"
    assert records[0]["exact_mismatch_slots"] == 8
    assert records[0]["max_budget_delta_kwh"] == old["max_budget_delta_kwh"]


def test_authoritative_plan_is_built_before_shadow_evaluation():
    source = Path(optimizer_engine.__file__).read_text(encoding="utf-8")
    plan_call = source.index("plan = build_virtual_plan(")
    shadow_call = source.index("shadow_summary = safe_evaluate_optimizer_shadow(")
    assert plan_call < shadow_call

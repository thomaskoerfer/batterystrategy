#!/usr/bin/env python3
"""Retrospective Battery Strategy backtest with a perfect-foresight benchmark.

The script uses the HACS command trace as measured reality, reconstructs the
grid residual without battery influence, and computes the best battery path for
that already-known residual and price curve.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

SLOT_SECONDS = 15 * 60
SLOT_H = 0.25
CAP_KWH = 6.0
DEFAULT_MIN_SOC_PCT = 5.0
DEFAULT_MAX_SOC_PCT = 100.0
DEFAULT_MAX_POWER_W = 2400.0
DEFAULT_ETA_RT = 0.80
ENERGY_STEP_KWH = 0.025
DEFAULT_PRICE_ENTITY = "sensor.electricity_price"
DEFAULT_TRACE = "/config/battery_strategy_command_trace.jsonl"
DEFAULT_DB = "/config/home-assistant_v2.db"
DEFAULT_TIBBER_POOL_GLOB = "/config/.storage/tibber_prices.interval_pool.*"


@dataclass(frozen=True)
class RawSample:
    ts: float
    grid_import_w: float
    grid_export_w: float
    battery_power_w: float
    pv_w: float
    ev_power_w: float
    soc_pct: float
    mode: str
    power_w: float
    reason: str


@dataclass(frozen=True)
class Slot:
    ts: int
    price_ct: float
    residual_with_ev_kwh: float
    dischargeable_load_kwh: float
    pv_surplus_kwh: float
    actual_grid_import_kwh: float
    actual_grid_export_kwh: float
    actual_charge_kwh: float
    actual_discharge_kwh: float
    actual_mode: str
    actual_reason: str
    soc_start_pct: float
    soc_end_pct: float
    samples: int


@dataclass(frozen=True)
class OptimizedSlot:
    ts: int
    price_ct: float
    residual_with_ev_kwh: float
    dischargeable_load_kwh: float
    actual_grid_import_kwh: float
    optimal_grid_import_kwh: float
    actual_grid_export_kwh: float
    optimal_grid_export_kwh: float
    optimal_charge_kwh: float
    optimal_discharge_kwh: float
    actual_charge_kwh: float
    actual_discharge_kwh: float
    soc_start_pct: float
    soc_end_pct: float
    actual_mode: str
    actual_reason: str
    slot_gap_eur: float


@dataclass(frozen=True)
class BacktestResult:
    slots: list[OptimizedSlot]
    baseline_cost_eur: float
    actual_cost_eur: float
    optimal_cost_eur: float
    actual_savings_eur: float
    optimal_savings_eur: float
    controllable_gap_eur: float
    start_soc_pct: float
    end_soc_pct: float
    target_end_soc_pct: float


def _float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.lower() in {"unknown", "unavailable", "none"}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def slot_start(ts: float) -> int:
    return int(ts // SLOT_SECONDS) * SLOT_SECONDS


def load_trace(path: str | Path) -> list[RawSample]:
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix == ".jsonl":
        raw = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        raw = json.loads(text)
    if isinstance(raw, dict):
        raw = raw.get("samples") or raw.get("trace") or []
    samples: list[RawSample] = []
    for item in raw:
        ts = _float(item.get("ts"))
        if ts <= 0:
            continue
        samples.append(
            RawSample(
                ts=ts,
                grid_import_w=max(0.0, _float(item.get("grid_import_w"))),
                grid_export_w=max(0.0, _float(item.get("grid_export_w"))),
                battery_power_w=_float(item.get("battery_power_w")),
                pv_w=max(0.0, _float(item.get("pv_w"))),
                ev_power_w=max(0.0, _float(item.get("ev_power_w"))),
                soc_pct=_float(item.get("soc_pct"), 50.0),
                mode=str(item.get("mode") or "idle"),
                power_w=max(0.0, _float(item.get("power_w"))),
                reason=str(item.get("reason") or ""),
            )
        )
    return sorted(samples, key=lambda sample: sample.ts)


def load_price_series(db_path: str | Path, entity_id: str, start_ts: float, end_ts: float) -> list[tuple[float, float]]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            """
            SELECT s.last_updated_ts, s.state
            FROM states s
            JOIN states_meta sm ON sm.metadata_id = s.metadata_id
            WHERE sm.entity_id = ?
              AND s.last_updated_ts >= ?
              AND s.last_updated_ts <= ?
            ORDER BY s.last_updated_ts
            """,
            (entity_id, float(start_ts) - 6 * 3600, float(end_ts) + 3600),
        ).fetchall()
    finally:
        con.close()

    series: list[tuple[float, float]] = []
    for ts, state in rows:
        value = _float(state, math.nan)
        if math.isnan(value):
            continue
        # Tibber Prices exposes EUR/kWh on some entities and ct/kWh on others.
        price_ct = value * 100.0 if value < 2.0 else value
        if 0.0 <= price_ct <= 200.0:
            series.append((float(ts), price_ct))
    return series


def load_tibber_pool_prices(pattern: str, start_ts: float, end_ts: float) -> list[tuple[float, float]]:
    series: list[tuple[float, float]] = []
    for path in sorted(Path("/").glob(pattern[1:]) if pattern.startswith("/") else Path(".").glob(pattern)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        groups = data.get("fetch_groups", []) if isinstance(data, dict) else []
        for group in groups:
            for interval in group.get("intervals", []):
                starts_at = interval.get("startsAt") or interval.get("start")
                total = interval.get("total")
                if starts_at is None or total is None:
                    continue
                try:
                    ts = datetime.fromisoformat(str(starts_at).replace("Z", "+00:00")).timestamp()
                    value = float(total)
                except (TypeError, ValueError):
                    continue
                price_ct = value * 100.0 if value < 2.0 else value
                if start_ts - 6 * 3600 <= ts <= end_ts + 3600 and 0.0 <= price_ct <= 200.0:
                    series.append((ts, price_ct))
    return sorted(set(series))


def price_at(series: list[tuple[float, float]], ts: float) -> float:
    if not series:
        raise ValueError("No usable price samples found")
    lo = 0
    hi = len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            lo = mid + 1
        else:
            hi = mid
    idx = max(0, lo - 1)
    return series[idx][1]


def _mode_reason(items: Iterable[RawSample]) -> tuple[str, str]:
    weights: dict[tuple[str, str], int] = {}
    for item in items:
        key = (item.mode, item.reason)
        weights[key] = weights.get(key, 0) + 1
    return max(weights.items(), key=lambda kv: kv[1])[0] if weights else ("idle", "")


def aggregate_slots(samples: list[RawSample], prices: list[tuple[float, float]], start_ts: float, end_ts: float) -> list[Slot]:
    selected = [sample for sample in samples if start_ts <= sample.ts <= end_ts]
    if len(selected) < 2:
        raise ValueError("Not enough trace samples in requested window")

    buckets: dict[int, list[tuple[RawSample, float]]] = {}
    for idx, sample in enumerate(selected):
        next_ts = selected[idx + 1].ts if idx + 1 < len(selected) else min(end_ts, sample.ts + 10)
        duration_s = max(0.0, min(60.0, next_ts - sample.ts))
        if duration_s <= 0:
            continue
        buckets.setdefault(slot_start(sample.ts), []).append((sample, duration_s))

    slots: list[Slot] = []
    previous_soc = selected[0].soc_pct
    for ts in sorted(buckets):
        rows = buckets[ts]
        duration_total = sum(duration for _, duration in rows)
        if duration_total <= 0:
            continue
        residual_kwh = 0.0
        dischargeable_kwh = 0.0
        actual_import_kwh = 0.0
        actual_export_kwh = 0.0
        actual_charge_kwh = 0.0
        actual_discharge_kwh = 0.0
        soc_weighted = 0.0
        samples_only = []
        for sample, duration_s in rows:
            weight_h = duration_s / 3600.0
            residual_w = sample.grid_import_w - sample.grid_export_w + sample.battery_power_w
            residual_kwh += residual_w / 1000.0 * weight_h
            dischargeable_kwh += max(0.0, residual_w - sample.ev_power_w) / 1000.0 * weight_h
            actual_import_kwh += sample.grid_import_w / 1000.0 * weight_h
            actual_export_kwh += sample.grid_export_w / 1000.0 * weight_h
            if sample.mode == "input":
                actual_charge_kwh += sample.power_w / 1000.0 * weight_h
            elif sample.mode == "output":
                actual_discharge_kwh += sample.power_w / 1000.0 * weight_h
            soc_weighted += sample.soc_pct * duration_s
            samples_only.append(sample)
        mode, reason = _mode_reason(samples_only)
        soc_avg = soc_weighted / duration_total
        slots.append(
            Slot(
                ts=ts,
                price_ct=price_at(prices, ts),
                residual_with_ev_kwh=residual_kwh,
                dischargeable_load_kwh=dischargeable_kwh,
                pv_surplus_kwh=max(0.0, -residual_kwh),
                actual_grid_import_kwh=actual_import_kwh,
                actual_grid_export_kwh=actual_export_kwh,
                actual_charge_kwh=actual_charge_kwh,
                actual_discharge_kwh=actual_discharge_kwh,
                actual_mode=mode,
                actual_reason=reason,
                soc_start_pct=previous_soc,
                soc_end_pct=soc_avg,
                samples=len(rows),
            )
        )
        previous_soc = soc_avg
    return slots


def cost_eur(import_kwh: float, export_kwh: float, price_ct: float, feed_in_ct: float) -> float:
    return ((import_kwh * price_ct) - (export_kwh * feed_in_ct)) / 100.0


def optimize_perfect_foresight(
    slots: list[Slot],
    *,
    start_soc_pct: float,
    target_end_soc_pct: float,
    min_soc_pct: float,
    max_soc_pct: float,
    max_power_w: float,
    eta_rt: float,
    feed_in_ct: float,
    allow_grid_charge: bool,
) -> BacktestResult:
    eta_c = math.sqrt(eta_rt)
    eta_d = math.sqrt(eta_rt)
    max_slot_kwh = max_power_w / 1000.0 * SLOT_H
    min_e = CAP_KWH * min_soc_pct / 100.0
    max_e = CAP_KWH * max_soc_pct / 100.0
    start_e = min(max_e, max(min_e, CAP_KWH * start_soc_pct / 100.0))
    target_end_e = min(max_e, max(min_e, CAP_KWH * target_end_soc_pct / 100.0))
    steps = int(round(CAP_KWH / ENERGY_STEP_KWH))

    def idx_to_e(idx: int) -> float:
        return idx * ENERGY_STEP_KWH

    def e_to_idx(e_kwh: float) -> int:
        return int(round(e_kwh / ENERGY_STEP_KWH))

    start_idx = e_to_idx(start_e)
    min_idx = e_to_idx(min_e)
    max_idx = e_to_idx(max_e)
    inf = 1e18
    dp = {start_idx: 0.0}
    prev: list[dict[int, tuple[int, float, float, float, float, float]]] = []

    for slot in slots:
        next_dp: dict[int, float] = {}
        back: dict[int, tuple[int, float, float, float, float, float]] = {}
        for idx, acc_cost_ct in dp.items():
            e = idx_to_e(idx)
            actions: list[tuple[float, float]] = [(0.0, 0.0)]

            max_charge_ac = min(max_slot_kwh, max(0.0, (max_e - e) / eta_c))
            if not allow_grid_charge:
                max_charge_ac = min(max_charge_ac, max(0.0, -slot.residual_with_ev_kwh))
            charge_steps = int(math.floor(max_charge_ac / ENERGY_STEP_KWH + 1e-9))
            for step in range(1, charge_steps + 1):
                actions.append((step * ENERGY_STEP_KWH, 0.0))

            max_discharge_ac = min(
                max_slot_kwh,
                max(0.0, slot.dischargeable_load_kwh),
                max(0.0, (e - min_e) * eta_d),
            )
            discharge_steps = int(math.floor(max_discharge_ac / ENERGY_STEP_KWH + 1e-9))
            for step in range(1, discharge_steps + 1):
                actions.append((0.0, step * ENERGY_STEP_KWH))

            for charge_ac, discharge_ac in actions:
                next_e = e + charge_ac * eta_c - discharge_ac / eta_d
                if next_e < min_e - 1e-9 or next_e > max_e + 1e-9:
                    continue
                next_idx = min(max_idx, max(min_idx, e_to_idx(next_e)))
                net_grid_kwh = slot.residual_with_ev_kwh + charge_ac - discharge_ac
                grid_import = max(0.0, net_grid_kwh)
                grid_export = max(0.0, -net_grid_kwh)
                step_cost_ct = grid_import * slot.price_ct - grid_export * feed_in_ct
                total = acc_cost_ct + step_cost_ct
                if total < next_dp.get(next_idx, inf):
                    next_dp[next_idx] = total
                    back[next_idx] = (idx, charge_ac, discharge_ac, grid_import, grid_export, step_cost_ct)
        dp = next_dp
        prev.append(back)

    final_candidates = {idx: cost for idx, cost in dp.items() if idx_to_e(idx) + 1e-9 >= target_end_e}
    if not final_candidates:
        final_candidates = dp
    final_idx = min(final_candidates, key=final_candidates.get)

    actions_rev = []
    idx = final_idx
    for back in reversed(prev):
        rec = back[idx]
        prev_idx, charge_ac, discharge_ac, grid_import, grid_export, step_cost_ct = rec
        actions_rev.append((idx, charge_ac, discharge_ac, grid_import, grid_export, step_cost_ct))
        idx = prev_idx
    actions = list(reversed(actions_rev))

    optimized: list[OptimizedSlot] = []
    baseline_cost = 0.0
    actual_cost = 0.0
    optimal_cost = 0.0
    soc_e = start_e
    for slot, action in zip(slots, actions, strict=True):
        next_idx, charge_ac, discharge_ac, opt_import, opt_export, step_cost_ct = action
        baseline_import = max(0.0, slot.residual_with_ev_kwh)
        baseline_export = max(0.0, -slot.residual_with_ev_kwh)
        baseline_cost += cost_eur(baseline_import, baseline_export, slot.price_ct, feed_in_ct)
        actual_slot_cost = cost_eur(slot.actual_grid_import_kwh, slot.actual_grid_export_kwh, slot.price_ct, feed_in_ct)
        optimal_slot_cost = step_cost_ct / 100.0
        actual_cost += actual_slot_cost
        optimal_cost += optimal_slot_cost
        soc_start_pct = soc_e / CAP_KWH * 100.0
        soc_e = idx_to_e(next_idx)
        soc_end_pct = soc_e / CAP_KWH * 100.0
        optimized.append(
            OptimizedSlot(
                ts=slot.ts,
                price_ct=slot.price_ct,
                residual_with_ev_kwh=slot.residual_with_ev_kwh,
                dischargeable_load_kwh=slot.dischargeable_load_kwh,
                actual_grid_import_kwh=slot.actual_grid_import_kwh,
                optimal_grid_import_kwh=opt_import,
                actual_grid_export_kwh=slot.actual_grid_export_kwh,
                optimal_grid_export_kwh=opt_export,
                optimal_charge_kwh=charge_ac,
                optimal_discharge_kwh=discharge_ac,
                actual_charge_kwh=slot.actual_charge_kwh,
                actual_discharge_kwh=slot.actual_discharge_kwh,
                soc_start_pct=soc_start_pct,
                soc_end_pct=soc_end_pct,
                actual_mode=slot.actual_mode,
                actual_reason=slot.actual_reason,
                slot_gap_eur=actual_slot_cost - optimal_slot_cost,
            )
        )

    return BacktestResult(
        slots=optimized,
        baseline_cost_eur=round(baseline_cost, 4),
        actual_cost_eur=round(actual_cost, 4),
        optimal_cost_eur=round(optimal_cost, 4),
        actual_savings_eur=round(baseline_cost - actual_cost, 4),
        optimal_savings_eur=round(baseline_cost - optimal_cost, 4),
        controllable_gap_eur=round(actual_cost - optimal_cost, 4),
        start_soc_pct=round(start_e / CAP_KWH * 100.0, 1),
        end_soc_pct=round(slots[-1].soc_end_pct, 1),
        target_end_soc_pct=round(target_end_soc_pct, 1),
    )


def summarize(result: BacktestResult, top_n: int) -> dict:
    charge_actual = sum(slot.actual_charge_kwh for slot in result.slots)
    charge_opt = sum(slot.optimal_charge_kwh for slot in result.slots)
    discharge_actual = sum(slot.actual_discharge_kwh for slot in result.slots)
    discharge_opt = sum(slot.optimal_discharge_kwh for slot in result.slots)
    import_actual = sum(slot.actual_grid_import_kwh for slot in result.slots)
    import_opt = sum(slot.optimal_grid_import_kwh for slot in result.slots)
    export_actual = sum(slot.actual_grid_export_kwh for slot in result.slots)
    export_opt = sum(slot.optimal_grid_export_kwh for slot in result.slots)
    worst = sorted(result.slots, key=lambda slot: slot.slot_gap_eur, reverse=True)[:top_n]
    return {
        "slots": len(result.slots),
        "start_soc_pct": result.start_soc_pct,
        "end_soc_pct": result.end_soc_pct,
        "target_end_soc_pct": result.target_end_soc_pct,
        "baseline_cost_eur": result.baseline_cost_eur,
        "actual_cost_eur": result.actual_cost_eur,
        "optimal_cost_eur": result.optimal_cost_eur,
        "actual_savings_eur": result.actual_savings_eur,
        "optimal_savings_eur": result.optimal_savings_eur,
        "controllable_gap_eur": result.controllable_gap_eur,
        "actual_charge_kwh": round(charge_actual, 3),
        "optimal_charge_kwh": round(charge_opt, 3),
        "actual_discharge_kwh": round(discharge_actual, 3),
        "optimal_discharge_kwh": round(discharge_opt, 3),
        "actual_grid_import_kwh": round(import_actual, 3),
        "optimal_grid_import_kwh": round(import_opt, 3),
        "actual_grid_export_kwh": round(export_actual, 3),
        "optimal_grid_export_kwh": round(export_opt, 3),
        "top_gaps": [
            {
                "time": datetime.fromtimestamp(slot.ts, timezone.utc).astimezone().isoformat(timespec="minutes"),
                "gap_eur": round(slot.slot_gap_eur, 4),
                "price_ct": round(slot.price_ct, 2),
                "actual_mode": slot.actual_mode,
                "actual_reason": slot.actual_reason,
                "actual_import_kwh": round(slot.actual_grid_import_kwh, 3),
                "optimal_import_kwh": round(slot.optimal_grid_import_kwh, 3),
                "actual_charge_kwh": round(slot.actual_charge_kwh, 3),
                "optimal_charge_kwh": round(slot.optimal_charge_kwh, 3),
                "actual_discharge_kwh": round(slot.actual_discharge_kwh, 3),
                "optimal_discharge_kwh": round(slot.optimal_discharge_kwh, 3),
                "soc_start_pct": round(slot.soc_start_pct, 1),
                "soc_end_pct": round(slot.soc_end_pct, 1),
            }
            for slot in worst
        ],
    }


def write_csv(result: BacktestResult, path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "time",
                "price_ct",
                "residual_with_ev_kwh",
                "dischargeable_load_kwh",
                "actual_import_kwh",
                "optimal_import_kwh",
                "actual_export_kwh",
                "optimal_export_kwh",
                "actual_charge_kwh",
                "optimal_charge_kwh",
                "actual_discharge_kwh",
                "optimal_discharge_kwh",
                "soc_start_pct",
                "soc_end_pct",
                "actual_mode",
                "actual_reason",
                "slot_gap_eur",
            ],
        )
        writer.writeheader()
        for slot in result.slots:
            writer.writerow(
                {
                    "time": datetime.fromtimestamp(slot.ts, timezone.utc).astimezone().isoformat(timespec="minutes"),
                    "price_ct": round(slot.price_ct, 3),
                    "residual_with_ev_kwh": round(slot.residual_with_ev_kwh, 4),
                    "dischargeable_load_kwh": round(slot.dischargeable_load_kwh, 4),
                    "actual_import_kwh": round(slot.actual_grid_import_kwh, 4),
                    "optimal_import_kwh": round(slot.optimal_grid_import_kwh, 4),
                    "actual_export_kwh": round(slot.actual_grid_export_kwh, 4),
                    "optimal_export_kwh": round(slot.optimal_grid_export_kwh, 4),
                    "actual_charge_kwh": round(slot.actual_charge_kwh, 4),
                    "optimal_charge_kwh": round(slot.optimal_charge_kwh, 4),
                    "actual_discharge_kwh": round(slot.actual_discharge_kwh, 4),
                    "optimal_discharge_kwh": round(slot.optimal_discharge_kwh, 4),
                    "soc_start_pct": round(slot.soc_start_pct, 2),
                    "soc_end_pct": round(slot.soc_end_pct, 2),
                    "actual_mode": slot.actual_mode,
                    "actual_reason": slot.actual_reason,
                    "slot_gap_eur": round(slot.slot_gap_eur, 5),
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", default=DEFAULT_TRACE)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--price-entity", default=DEFAULT_PRICE_ENTITY)
    parser.add_argument("--tibber-pool-glob", default=DEFAULT_TIBBER_POOL_GLOB)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--end", help="ISO timestamp; defaults to latest trace sample")
    parser.add_argument("--min-soc-pct", type=float, default=DEFAULT_MIN_SOC_PCT)
    parser.add_argument("--max-soc-pct", type=float, default=DEFAULT_MAX_SOC_PCT)
    parser.add_argument("--max-power-w", type=float, default=DEFAULT_MAX_POWER_W)
    parser.add_argument("--eta-rt", type=float, default=DEFAULT_ETA_RT)
    parser.add_argument("--feed-in-ct", type=float, default=0.0)
    parser.add_argument("--no-grid-charge", action="store_true")
    parser.add_argument("--target-end-soc", choices=["actual", "start", "min"], default="actual")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--csv-out")
    parser.add_argument("--json-out")
    return parser.parse_args()


def _parse_end(value: str | None, fallback: float) -> float:
    if not value:
        return fallback
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def main() -> int:
    args = parse_args()
    samples = load_trace(args.trace)
    if not samples:
        raise SystemExit("No trace samples found")
    end_ts = _parse_end(args.end, samples[-1].ts)
    start_ts = end_ts - args.hours * 3600.0
    prices = load_price_series(args.db, args.price_entity, start_ts, end_ts)
    if not prices:
        prices = load_tibber_pool_prices(args.tibber_pool_glob, start_ts, end_ts)
    slots = aggregate_slots(samples, prices, start_ts, end_ts)
    if args.target_end_soc == "start":
        target_end_soc = slots[0].soc_start_pct
    elif args.target_end_soc == "min":
        target_end_soc = args.min_soc_pct
    else:
        target_end_soc = slots[-1].soc_end_pct
    result = optimize_perfect_foresight(
        slots,
        start_soc_pct=slots[0].soc_start_pct,
        target_end_soc_pct=target_end_soc,
        min_soc_pct=args.min_soc_pct,
        max_soc_pct=args.max_soc_pct,
        max_power_w=args.max_power_w,
        eta_rt=args.eta_rt,
        feed_in_ct=args.feed_in_ct,
        allow_grid_charge=not args.no_grid_charge,
    )
    summary = summarize(result, args.top)
    payload = {"summary": summary}
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.csv_out:
        write_csv(result, args.csv_out)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

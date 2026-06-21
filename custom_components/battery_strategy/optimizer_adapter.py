"""Adapter for the full-quality internal optimizer engine."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import time
from dataclasses import asdict

from .const import COMMAND_IDLE, COMMAND_INPUT, COMMAND_OUTPUT
from .models import StrategyInputs, StrategyOptions
from .plan_models import DailyCost, PlanPoint, StrategyPlan

CACHE_TTL_S = 240
SLOT_MS = 15 * 60 * 1000


class OptimizerEngineAdapter:
    """Run the high-quality optimizer inside the HACS integration."""

    def __init__(self) -> None:
        """Initialize adapter cache."""
        self._last_run_ts = 0.0
        self._last_output: dict | None = None

    def run(self, inputs: StrategyInputs, options: StrategyOptions, force: bool = False) -> tuple[StrategyPlan, dict]:
        """Return a high-quality plan and raw optimizer attributes."""
        now = time.time()
        if not force and self._last_output is not None and now - self._last_run_ts < CACHE_TTL_S:
            return _plan_from_output(self._last_output, inputs, options), self._last_output

        from . import optimizer_engine as engine

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            engine.main()
        raw = buf.getvalue().strip().splitlines()
        output = json.loads(raw[-1]) if raw else {}
        self._last_output = output
        self._last_run_ts = now
        return _plan_from_output(output, inputs, options), output

    def age_s(self) -> float | None:
        """Return seconds since the last optimizer run."""
        if self._last_run_ts <= 0.0:
            return None
        return max(0.0, time.time() - self._last_run_ts)


def raw_attrs_from_plan(plan: StrategyPlan) -> dict:
    """Return an attribute dict compatible with existing comparison helpers."""
    return asdict(plan)


def _plan_from_output(output: dict, inputs: StrategyInputs, options: StrategyOptions) -> StrategyPlan:
    points = _points_from_output(output)
    today = _date_from_points(points, 0)
    tomorrow = _date_from_points(points, 1)
    daily_costs = {}
    if today:
        daily_costs[today] = DailyCost(
            _float(output.get("baseline_cost_today_eur")),
            _float(output.get("optimized_cost_today_eur")),
        )
    if tomorrow:
        daily_costs[tomorrow] = DailyCost(
            _float(output.get("baseline_cost_tomorrow_eur")),
            _float(output.get("optimized_cost_tomorrow_eur")),
        )
    mode = _mode_to_command(str(output.get("mode") or output.get("planned_mode") or "idle"))
    return StrategyPlan(
        points=points,
        current_mode=mode,
        current_power_w=int(round(_float(output.get("recommended_power_w", output.get("planned_power_w", 0))))),
        reason=str(output.get("reason") or "optimizer_engine"),
        daily_costs=daily_costs,
        price_stats={
            "min": _maybe_float(output.get("price_min_ct")),
            "max": _maybe_float(output.get("price_max_ct")),
            "avg": _maybe_float(output.get("price_avg_ct")),
            "p_low": _maybe_float(output.get("price_low_ct")),
            "p_high": _maybe_float(output.get("price_high_ct")),
            "terminal_value_ct": _maybe_float(output.get("terminal_value_ct")),
            "discharge_floor_ct": _maybe_float(output.get("discharge_floor_ct")),
        },
        load_forecast_next_1h_kwh=_float(output.get("load_forecast_next_1h_kwh")),
        pv_forecast_corrected_next_1h_kwh=_float(output.get("pv_forecast_corrected_next_1h_kwh")),
        net_load_forecast_next_1h_kwh=_float(output.get("net_load_forecast_next_1h_kwh")),
        grid_import_forecast_next_1h_kwh=_float(output.get("grid_import_forecast_next_1h_kwh")),
        grid_export_forecast_next_1h_kwh=_float(output.get("grid_export_forecast_next_1h_kwh")),
        virtual_soc_end_tomorrow_pct=_float(output.get("virtual_soc_end_tomorrow_pct")),
        override_active=options.manual_mode != "off",
    )


def _points_from_output(output: dict, now_ms: int | None = None) -> list[PlanPoint]:
    price = _series(output.get("profile_48h_price")) or _merge_series(
        _series(output.get("profile_today_price")),
        _series(output.get("profile_tomorrow_price")),
    )
    soc = _merge_series(_series(output.get("profile_today_soc")), _series(output.get("profile_tomorrow_soc")))
    power = _merge_series(_series(output.get("profile_today_power")), _series(output.get("profile_tomorrow_power")))
    charge = _series(output.get("profile_48h_charge_fc_power")) or _merge_series(
        _series(output.get("profile_today_charge_power")),
        _series(output.get("profile_tomorrow_charge_power")),
    )
    discharge = _series(output.get("profile_48h_discharge_fc_power")) or _merge_series(
        _series(output.get("profile_today_discharge_power")),
        _series(output.get("profile_tomorrow_discharge_power")),
    )
    discharge_budget = _series(output.get("profile_48h_discharge_budget_kwh")) or _merge_series(
        _series(output.get("profile_today_discharge_budget_kwh")),
        _series(output.get("profile_tomorrow_discharge_budget_kwh")),
    )
    pv = _series(output.get("profile_48h_pv_fc_power"))
    load = _series(output.get("profile_48h_house_fc_power"))
    grid_import = _series(output.get("profile_48h_grid_import_fc_power"))
    grid_export = _series(output.get("profile_48h_grid_export_fc_power"))
    grid_net = _series(output.get("profile_48h_grid_net_fc_power"))
    ts_values = sorted({*load, *pv, *grid_import, *grid_export, *grid_net, *charge, *discharge, *soc, *power})
    points = []
    for ts_ms in ts_values:
        ch = _at(charge, ts_ms)
        dis = _at(discharge, ts_ms)
        pow_w = _at(power, ts_ms) or max(ch, dis)
        mode = COMMAND_INPUT if ch > 0 else COMMAND_OUTPUT if dis > 0 else COMMAND_IDLE
        slot_dt = dt.datetime.fromtimestamp(ts_ms / 1000.0).astimezone()
        points.append(
            PlanPoint(
                ts_ms=ts_ms,
                date=slot_dt.date().isoformat(),
                price_ct=_at(price, ts_ms),
                load_fc_w=int(round(_at(load, ts_ms))),
                pv_fc_w=int(round(_at(pv, ts_ms))),
                grid_import_fc_w=int(round(_at(grid_import, ts_ms))),
                grid_export_fc_w=int(round(_at(grid_export, ts_ms))),
                grid_net_fc_w=int(round(_at(grid_net, ts_ms))),
                mode=mode,
                power_w=int(round(abs(pow_w))),
                charge_fc_w=int(round(ch)),
                discharge_fc_w=int(round(dis)),
                soc_pct=round(_at(soc, ts_ms), 2),
                discharge_budget_kwh=round(_at(discharge_budget, ts_ms), 3),
            )
        )
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return [point for point in points if point.ts_ms + SLOT_MS > now_ms]


def _series(raw) -> dict[int, float]:
    result = {}
    for item in raw or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            result[int(float(item[0]))] = float(item[1])
        except (TypeError, ValueError):
            continue
    return result


def _merge_series(*series_items: dict[int, float]) -> dict[int, float]:
    merged: dict[int, float] = {}
    for series in series_items:
        merged.update(series)
    return merged


def _at(series: dict[int, float], ts_ms: int) -> float:
    if ts_ms in series:
        return float(series[ts_ms])
    if not series:
        return 0.0
    best = min(series, key=lambda item_ts: abs(item_ts - ts_ms))
    return float(series[best]) if abs(best - ts_ms) <= 20 * 60 * 1000 else 0.0


def _date_from_points(points: list[PlanPoint], index: int) -> str | None:
    dates = []
    for point in points:
        if point.date not in dates:
            dates.append(point.date)
    return dates[index] if len(dates) > index else None


def _mode_to_command(mode: str) -> str:
    if mode in ("charge_grid", "charge_pv_surplus", "charge_follow", "input"):
        return COMMAND_INPUT
    if mode.startswith("discharge") or mode == "output":
        return COMMAND_OUTPUT
    return COMMAND_IDLE


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

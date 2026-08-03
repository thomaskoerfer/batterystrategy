"""Pure 48h optimizer for Battery Strategy."""

from __future__ import annotations

import datetime as dt

from .const import (
    COMMAND_IDLE,
    COMMAND_INPUT,
    COMMAND_OUTPUT,
    DISCHARGE_LOAD,
    DISCHARGE_PRICE_SENSITIVE,
    GRID_CHARGING_PRICE_SENSITIVE,
    MANUAL_CHARGE,
    MANUAL_DISCHARGE,
    PV_CHARGING_ON,
)
from .forecast import SLOTS_PER_HOUR, build_forecast_points, forecast_energy_kwh
from .models import StrategyInputs, StrategyOptions
from .plan_models import DailyCost, ForecastPoint, PlanPoint, PricePoint, StrategyPlan
from .pricing import price_stats

SLOT_H = 0.25


def build_optimizer_plan(
    inputs: StrategyInputs,
    options: StrategyOptions,
    now: dt.datetime,
    prices: list[PricePoint] | None = None,
    forecast_points: list[ForecastPoint] | None = None,
) -> StrategyPlan:
    """Build a 48h battery plan and current planned command."""
    prices = prices or []
    forecast = forecast_points or build_forecast_points(inputs, options, now, prices)
    stats = price_stats(prices or [PricePoint(p.ts_ms, p.price_ct) for p in forecast])
    low = _num(stats.get("p_low"), 25.0)
    high = _num(stats.get("p_high"), 35.0)
    charge_threshold = low
    discharge_threshold = max(high, charge_threshold + float(options.min_margin_ct_per_kwh))
    capacity_kwh = max(0.5, float(options.battery_capacity_kwh))
    min_e = capacity_kwh * max(0.0, float(options.min_soc_pct)) / 100.0
    max_e = capacity_kwh * max(0.0, float(options.max_soc_pct)) / 100.0
    energy = min(max(capacity_kwh * float(inputs.soc_pct) / 100.0, min_e), max_e)
    points: list[PlanPoint] = []

    for fc in forecast:
        mode, power = _slot_decision(fc, options, energy, min_e, max_e, charge_threshold, discharge_threshold)
        if mode == COMMAND_INPUT:
            energy = min(max_e, energy + (power / 1000.0) * SLOT_H * _charge_eff(options))
        elif mode == COMMAND_OUTPUT:
            energy = max(min_e, energy - (power / 1000.0) * SLOT_H / _discharge_eff(options))

        grid_net = int(round(fc.load_w - fc.pv_w + (power if mode == COMMAND_INPUT else -power if mode == COMMAND_OUTPUT else 0)))
        grid_import = max(0, grid_net)
        grid_export = max(0, -grid_net)
        slot_dt = dt.datetime.fromtimestamp(fc.ts_ms / 1000.0, tz=now.tzinfo)
        points.append(
            PlanPoint(
                ts_ms=fc.ts_ms,
                date=slot_dt.date().isoformat(),
                price_ct=round(float(fc.price_ct), 3),
                load_fc_w=int(round(fc.load_w)),
                pv_fc_w=int(round(fc.pv_w)),
                grid_import_fc_w=grid_import,
                grid_export_fc_w=grid_export,
                grid_net_fc_w=grid_net,
                mode=mode,
                power_w=int(round(power)),
                charge_fc_w=int(round(power if mode == COMMAND_INPUT else 0)),
                discharge_fc_w=int(round(power if mode == COMMAND_OUTPUT else 0)),
                soc_pct=round((energy / capacity_kwh) * 100.0, 2),
            )
        )

    points = _apply_manual_override(points, inputs, options)
    first = points[0] if points else None
    daily_costs = _daily_costs(points, options)
    today = now.date().isoformat()
    tomorrow = (now + dt.timedelta(days=1)).date().isoformat()
    tomorrow_points = [p for p in points if p.date == tomorrow]
    return StrategyPlan(
        points=points,
        current_mode=first.mode if first else COMMAND_IDLE,
        current_power_w=first.power_w if first else 0,
        reason=_reason(first, options),
        daily_costs=daily_costs,
        price_stats=stats,
        load_forecast_next_1h_kwh=forecast_energy_kwh(forecast, "load_w"),
        pv_forecast_corrected_next_1h_kwh=forecast_energy_kwh(forecast, "pv_w"),
        net_load_forecast_next_1h_kwh=round(
            sum((p.load_w - p.pv_w) for p in forecast[:SLOTS_PER_HOUR]) / 1000.0 / SLOTS_PER_HOUR,
            3,
        ),
        grid_import_forecast_next_1h_kwh=round(
            sum(max(0.0, p.load_w - p.pv_w) for p in forecast[:SLOTS_PER_HOUR]) / 1000.0 / SLOTS_PER_HOUR,
            3,
        ),
        grid_export_forecast_next_1h_kwh=round(
            sum(max(0.0, p.pv_w - p.load_w) for p in forecast[:SLOTS_PER_HOUR]) / 1000.0 / SLOTS_PER_HOUR,
            3,
        ),
        virtual_soc_end_tomorrow_pct=tomorrow_points[-1].soc_pct if tomorrow_points else (points[-1].soc_pct if points else 0.0),
        override_active=options.manual_mode in (MANUAL_CHARGE, MANUAL_DISCHARGE),
    )


def _slot_decision(
    fc: ForecastPoint,
    options: StrategyOptions,
    energy_kwh: float,
    min_e: float,
    max_e: float,
    charge_threshold_ct: float,
    discharge_threshold_ct: float,
) -> tuple[str, float]:
    pv_surplus = max(0.0, fc.pv_w - fc.load_w)
    load_deficit = max(0.0, fc.load_w - fc.pv_w)
    if options.pv_charging == PV_CHARGING_ON and pv_surplus > 0.0 and energy_kwh < max_e:
        return COMMAND_INPUT, min(float(options.max_charge_power_w), pv_surplus, _charge_room_w(energy_kwh, max_e, options))
    if (
        options.grid_charging == GRID_CHARGING_PRICE_SENSITIVE
        and fc.price_ct <= charge_threshold_ct
        and energy_kwh < max_e
    ):
        return COMMAND_INPUT, min(float(options.max_charge_power_w), _charge_room_w(energy_kwh, max_e, options))
    if options.discharge == DISCHARGE_LOAD and load_deficit > 0.0 and energy_kwh > min_e:
        return COMMAND_OUTPUT, min(float(options.max_discharge_power_w), load_deficit, _discharge_room_w(energy_kwh, min_e, options))
    if (
        options.discharge == DISCHARGE_PRICE_SENSITIVE
        and load_deficit > 0.0
        and fc.price_ct >= discharge_threshold_ct
        and energy_kwh > min_e
    ):
        return COMMAND_OUTPUT, min(float(options.max_discharge_power_w), load_deficit, _discharge_room_w(energy_kwh, min_e, options))
    return COMMAND_IDLE, 0.0


def _apply_manual_override(points: list[PlanPoint], inputs: StrategyInputs, options: StrategyOptions) -> list[PlanPoint]:
    if not points or options.manual_mode not in (MANUAL_CHARGE, MANUAL_DISCHARGE):
        return points
    first = points[0]
    if options.manual_mode == MANUAL_CHARGE and float(inputs.soc_pct) < float(options.max_soc_pct):
        power = int(round(min(float(options.manual_power_w), float(options.max_charge_power_w))))
        mode = COMMAND_INPUT if power > 0 else COMMAND_IDLE
    elif options.manual_mode == MANUAL_DISCHARGE and float(inputs.soc_pct) > float(options.min_soc_pct):
        power = int(round(min(float(options.manual_power_w), float(options.max_discharge_power_w))))
        mode = COMMAND_OUTPUT if power > 0 else COMMAND_IDLE
    else:
        power = 0
        mode = COMMAND_IDLE
    replacement = PlanPoint(
        first.ts_ms,
        first.date,
        first.price_ct,
        first.load_fc_w,
        first.pv_fc_w,
        first.grid_import_fc_w,
        first.grid_export_fc_w,
        first.grid_net_fc_w,
        mode,
        power,
        power if mode == COMMAND_INPUT else 0,
        power if mode == COMMAND_OUTPUT else 0,
        first.soc_pct,
    )
    return [replacement] + points[1:]


def _daily_costs(points: list[PlanPoint], options: StrategyOptions) -> dict[str, DailyCost]:
    days: dict[str, list[PlanPoint]] = {}
    for point in points:
        days.setdefault(point.date, []).append(point)
    result: dict[str, DailyCost] = {}
    export_ct = max(0.0, float(options.feed_in_tariff_ct_per_kwh))
    for day, arr in days.items():
        base = sum(
            ((max(0, p.load_fc_w - p.pv_fc_w) * p.price_ct) - (max(0, p.pv_fc_w - p.load_fc_w) * export_ct))
            / 1000.0
            * SLOT_H
            / 100.0
            for p in arr
        )
        optimized = sum(
            ((p.grid_import_fc_w * p.price_ct) - (p.grid_export_fc_w * export_ct)) / 1000.0 * SLOT_H / 100.0
            for p in arr
        )
        result[day] = DailyCost(round(base, 3), round(optimized, 3))
    return result


def _reason(first: PlanPoint | None, options: StrategyOptions) -> str:
    if options.manual_mode == MANUAL_CHARGE:
        return "manual_charge"
    if options.manual_mode == MANUAL_DISCHARGE:
        return "manual_discharge"
    if first is None or first.mode == COMMAND_IDLE:
        return "optimizer_idle"
    if first.mode == COMMAND_INPUT:
        return "optimizer_charge"
    return "optimizer_discharge"


def _charge_eff(options: StrategyOptions) -> float:
    return max(0.5, float(options.round_trip_efficiency) ** 0.5)


def _discharge_eff(options: StrategyOptions) -> float:
    return max(0.5, float(options.round_trip_efficiency) ** 0.5)


def _charge_room_w(energy: float, max_e: float, options: StrategyOptions) -> float:
    return max(0.0, (max_e - energy) / SLOT_H / _charge_eff(options) * 1000.0)


def _discharge_room_w(energy: float, min_e: float, options: StrategyOptions) -> float:
    return max(0.0, (energy - min_e) * _discharge_eff(options) / SLOT_H * 1000.0)


def _num(value: float | None, fallback: float) -> float:
    return fallback if value is None else float(value)

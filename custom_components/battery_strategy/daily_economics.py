"""Full-day cost projections from measured actuals and future plan slots.

This presentation boundary never influences optimization or live control. It
publishes a day only when every physical local-time slot has exactly one source:
finalized actual evidence or a future plan point.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .contracts import HistoricalFeatureSlot
from .plan_models import DailyCost, PlanPoint, StrategyPlan


def build_daily_cost_projection(
    history: Sequence[HistoricalFeatureSlot],
    plan: StrategyPlan,
    *,
    local_date: dt.date,
    as_of_ms: int,
    timezone: str,
    export_value_ct_per_kwh: float,
    actual_savings_today_eur: float,
) -> Mapping[str, DailyCost]:
    """Return complete calendar-day economics for today and tomorrow.

    EV consumption is excluded from the observed optimized grid position. The
    measured-savings ledger is authoritative for the completed day's battery
    benefit; feature-store power integration is not a second savings ledger.
    A missing finalized slot or price makes that calendar day unavailable
    instead of silently publishing a partial sum.
    """
    zone = ZoneInfo(timezone)
    today = local_date.isoformat()
    tomorrow_date = local_date + dt.timedelta(days=1)
    tomorrow = tomorrow_date.isoformat()
    relevant_actuals = _today_actuals(history, local_date, as_of_ms, zone)
    actual_by_start = {item.slot.start_ms: item for item in relevant_actuals}
    plan_by_start = {point.ts_ms: point for point in plan.points}
    result: dict[str, DailyCost] = {}

    today_cost = _complete_day_cost(
        local_date,
        zone,
        actual_by_start=actual_by_start,
        plan_by_start=plan_by_start,
        allow_actual=True,
        as_of_ms=as_of_ms,
        export_value_ct_per_kwh=export_value_ct_per_kwh,
        actual_savings_eur=actual_savings_today_eur,
    )
    if today_cost is not None:
        result[today] = today_cost

    tomorrow_cost = _complete_day_cost(
        tomorrow_date,
        zone,
        actual_by_start={},
        plan_by_start=plan_by_start,
        allow_actual=False,
        as_of_ms=as_of_ms,
        export_value_ct_per_kwh=export_value_ct_per_kwh,
        actual_savings_eur=0.0,
    )
    if tomorrow_cost is not None:
        result[tomorrow] = tomorrow_cost
    return MappingProxyType(result)


def _today_actuals(
    history: Sequence[HistoricalFeatureSlot],
    local_date: dt.date,
    as_of_ms: int,
    zone: ZoneInfo,
) -> tuple[HistoricalFeatureSlot, ...]:
    start_ms, end_ms = _day_bounds_ms(local_date, zone)
    selected = []
    # Feature history is chronological. Walking backwards bounds normal HA
    # refresh work to at most one local day instead of the full retention set.
    for item in reversed(history):
        if item.slot.start_ms < start_ms:
            break
        if item.slot.start_ms >= end_ms or item.slot.end_ms > as_of_ms:
            continue
        selected.append(item)
    return tuple(reversed(selected))


def _complete_day_cost(
    day: dt.date,
    zone: ZoneInfo,
    *,
    actual_by_start: Mapping[int, HistoricalFeatureSlot],
    plan_by_start: Mapping[int, PlanPoint],
    allow_actual: bool,
    as_of_ms: int,
    export_value_ct_per_kwh: float,
    actual_savings_eur: float,
) -> DailyCost | None:
    base_eur = 0.0
    with_bat_eur = 0.0
    for start_ms in _day_slot_starts(day, zone):
        actual = actual_by_start.get(start_ms) if allow_actual else None
        if actual is not None:
            if actual.price_ct_per_kwh is None:
                return None
            optimized_net_kwh = (
                actual.grid_import_kwh - actual.grid_export_kwh - actual.ev_charge_kwh
            )
            price_ct = float(actual.price_ct_per_kwh)
            # The direct battery energy counters own measured savings. Start
            # both actual cost paths at observed grid cost and add the ledger
            # benefit to the no-battery baseline once after all slots.
            baseline_net_kwh = optimized_net_kwh
        else:
            point = plan_by_start.get(start_ms)
            # Past slots require finalized evidence; future slots require a plan.
            if point is None or (allow_actual and start_ms + 900_000 <= as_of_ms):
                return None
            slot_h = 0.25
            baseline_net_kwh = (point.load_fc_w - point.pv_fc_w) / 1000.0 * slot_h
            optimized_net_kwh = (
                (point.grid_import_fc_w - point.grid_export_fc_w) / 1000.0 * slot_h
            )
            price_ct = point.price_ct
        base_eur += _grid_cost(baseline_net_kwh, price_ct, export_value_ct_per_kwh)
        with_bat_eur += _grid_cost(optimized_net_kwh, price_ct, export_value_ct_per_kwh)
    if allow_actual:
        base_eur += float(actual_savings_eur)
    return DailyCost(round(base_eur, 3), round(with_bat_eur, 3))


def _day_slot_starts(day: dt.date, zone: ZoneInfo) -> tuple[int, ...]:
    start_ms, end_ms = _day_bounds_ms(day, zone)
    return tuple(range(start_ms, end_ms, 900_000))


def _day_bounds_ms(day: dt.date, zone: ZoneInfo) -> tuple[int, int]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    return round(start.timestamp() * 1000), round(end.timestamp() * 1000)


def _grid_cost(net_kwh: float, import_ct: float, export_ct: float) -> float:
    return (max(0.0, net_kwh) * import_ct - max(0.0, -net_kwh) * export_ct) / 100.0

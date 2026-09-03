#!/usr/bin/env python3
"""Home Assistant-facing orchestration for one planning pipeline run."""

import datetime as dt
import math

from .forecast_application import (
    bootstrap_samples_from_features,
    build_forecast_targets,
    build_production_forecast,
    weather_factor_from_cloud_rad,
    weather_snapshot,
)
from .forecast_evaluation import update_forecast_evaluation
from .market_context import MarketContextConfig, MarketContextService
from .plan_presentation import (
    build_price_profile,
    build_published_plan_profiles,
    derive_planned_dispatch,
)
from .planning_runtime import PlanningRuntime, PlanningRuntimeSettings
from .planning_service import PlanningService, PlanningSettings
from .planning_state import (
    advance_virtual_energy,
    append_virtual_trace,
    fallback_output,
    load_state,
    normalize_slot_biases,
    save_state,
)
from .runtime_market_data import (
    build_tibber_price_index,
    local_date_set_between,
    read_tibber_future_price_stats,
    read_tibber_intervals_for_dates,
    tibber_price_eur_at,
)
from .runtime_measurements import (
    E_BATTERY_INPUT_ENERGY,
    E_BATTERY_MIN_SOC,
    E_BATTERY_OUTPUT_ENERGY,
    E_BATTERY_POWER,
    E_BATTERY_SOC,
    E_EV_POWER,
    E_EV_STATUS,
    E_GRID_EXPORT,
    E_GRID_IMPORT,
    E_HEAT_PUMP_POWER,
    E_PRICE_CURRENT,
    E_PRICE_EUR,
    E_PV_NEXT_HOUR_ENERGY,
    E_PV_NEXT_HOUR_POWER,
    E_PV_POWER,
    E_PV_TOMORROW_ENERGY,
    E_WEATHER_CLOUD,
    E_WEATHER_RADIATION,
    as_float,
    fetch_house_actual_profile,
    fetch_net_actual_profile,
    fetch_pv_actual_profile,
    fetch_sensor_series_many,
    get_latest_states,
    net_no_battery_no_ev_w,
    net_no_battery_with_ev_w,
    real_charge_follow_surplus_w,
)
from .savings import SavingsConfig, SavingsEntities, SavingsLedger

SLOT_H = 0.25
HISTORY_DAYS = 60
SLOTS_PER_DAY = 96
EEX_CACHE_TTL_S = 6 * 3600
TERMINAL_RANK_THRESHOLD = 0.35
TERMINAL_VALUE_CAP_CT = 25.0
PV_RECOVERY_CONFIDENCE = 0.75
PV_RECOVERY_RESERVE_KWH = 0.30
EEX_PROXY_MIN_FULL_DAY_SLOTS = 90
EEX_PROXY_RECENT_DAYS = 5
EEX_PROXY_MIN_RETAIL_MARKUP_CT = 18.0
EEX_PROXY_MAX_BASE_RETAIL_MARKUP_CT = 28.0
EEX_PROXY_MAX_PEAK_RETAIL_MARKUP_CT = 32.0
EEX_PROXY_MIN_PRICE_CT = 12.0
EEX_PROXY_MAX_PRICE_CT = 70.0
# PV surplus anti-cycling thresholds
PV_SURPLUS_START_AVG_W = 50.0
PV_SURPLUS_MIN_SAMPLE_W = 40.0
PV_SURPLUS_REQUIRED_COUNT = 1
PV_SURPLUS_WINDOW_SAMPLES = 1


def _market_context_service(settings: PlanningRuntimeSettings) -> MarketContextService:
    """Build the setup-neutral market boundary from current runtime options."""
    return MarketContextService(
        MarketContextConfig(
            timezone=settings.timezone,
            round_trip_efficiency=settings.round_trip_efficiency,
            min_margin_ct_per_kwh=settings.min_margin_ct_per_kwh,
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


def _planning_service(settings: PlanningRuntimeSettings) -> PlanningService:
    return PlanningService(
        market_context=_market_context_service(settings),
        settings=PlanningSettings(
            battery_capacity_kwh=settings.battery_capacity_kwh,
            min_soc_pct=settings.min_soc_pct,
            max_soc_pct=settings.max_soc_pct,
            max_charge_power_w=settings.max_charge_power_w,
            max_discharge_power_w=settings.max_discharge_power_w,
            round_trip_efficiency=settings.round_trip_efficiency,
            min_margin_ct_per_kwh=settings.min_margin_ct_per_kwh,
            export_opportunity_ct_per_kwh=settings.export_opportunity_ct_per_kwh,
            pv_charging_allowed=settings.pv_charging_allowed,
            grid_charging_allowed=settings.grid_charging_allowed,
            discharge_allowed=settings.discharge_allowed,
            pv_recovery_confidence=PV_RECOVERY_CONFIDENCE,
            pv_recovery_reserve_kwh=PV_RECOVERY_RESERVE_KWH,
            slot_hours=SLOT_H,
        ),
    )


def _update_actual_savings(runtime, data, now_ts):
    """Update measured savings through its independent accounting boundary."""
    return SavingsLedger(
        config=SavingsConfig(
            timezone=runtime.settings.timezone,
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
        history_reader=lambda entities, cutoff: fetch_sensor_series_many(
            runtime, entities, cutoff
        ),
        price_reader=lambda dates: read_tibber_intervals_for_dates(runtime, dates),
    ).update(data, now_ts)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def slot_index_for_dt(dt_obj):
    return dt_obj.hour * 4 + dt_obj.minute // 15


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


def floor_to_quarter(dt_obj):
    return dt_obj.replace(minute=(dt_obj.minute // 15) * 15, second=0, microsecond=0)


def collect_inputs(runtime):
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
    s = get_latest_states(runtime, needed)

    price_ts, price_vals = build_tibber_price_index(
        runtime,
        local_date_set_between(
            runtime.settings.timezone,
            dt.datetime.now(dt.timezone.utc).timestamp(),
            dt.datetime.now(dt.timezone.utc).timestamp(),
        ),
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
    weather = weather_snapshot(
        runtime, int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    )
    if weather:
        cloud = weather["cloud_cover"]
        rad = weather["shortwave_radiation"]

    future_stats = read_tibber_future_price_stats(
        runtime, dt.datetime.now(runtime.settings.timezone)
    )
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
        "soc_min_pct": as_float(s[E_BATTERY_MIN_SOC], runtime.settings.min_soc_pct),
        "hp_w": as_float(s[E_HEAT_PUMP_POWER], 0.0),
        "pv_raw_kwh": pv_raw_kwh,
        "pv_tomorrow_kwh": as_float(s[E_PV_TOMORROW_ENERGY], None),
        "cloud": 50.0 if cloud is None else cloud,
        "rad": 0.0 if rad is None else rad,
        "weather": weather,
    }


def run(runtime_context):
    """Execute one planning refresh from an explicitly captured snapshot."""
    runtime = PlanningRuntime.from_mapping(runtime_context)
    settings = runtime.settings
    now = dt.datetime.now(dt.timezone.utc)
    now_ts = now.timestamp()
    local_now = dt.datetime.now(settings.timezone)
    today = local_now.date().isoformat()
    tomorrow = (local_now.date() + dt.timedelta(days=1)).isoformat()

    data = load_state(runtime)
    inputs = collect_inputs(runtime)
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
    soc_min_pct = clamp(
        float(inputs.get("soc_min_pct", settings.min_soc_pct)), 0.0, 40.0
    )
    settings = settings.with_min_soc_pct(soc_min_pct)
    hp_w = inputs["hp_w"]
    pv_raw_kwh = inputs["pv_raw_kwh"]
    pv_tomorrow_kwh = inputs["pv_tomorrow_kwh"]
    cloud = inputs["cloud"]
    rad = inputs["rad"]

    if len(data.get("samples", [])) < 120:
        data["samples"] = bootstrap_samples_from_features(runtime, now_ts, days=21)

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
    ) = _update_actual_savings(runtime, data, now_ts)

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
    rte_break_even_ct = (
        (p_now / settings.round_trip_efficiency) + settings.min_margin_ct_per_kwh
        if p_now is not None
        else None
    )
    expected_spread_ct = (
        (p_future_max * settings.round_trip_efficiency) - p_now
        if p_now is not None
        else None
    )

    mode = "idle"
    rec_w = 0
    reason = "15min Tibber plan"

    evaluation = update_forecast_evaluation(
        data,
        now_ts=now_ts,
        local_timezone=local_now.tzinfo,
        round_trip_efficiency=settings.round_trip_efficiency,
        retention_days=HISTORY_DAYS,
    )
    bt24 = evaluation.last_24h
    bt7d = evaluation.last_7d
    hit24 = evaluation.hit_rate_24h_pct

    market_context = _market_context_service(settings)
    eex_days = market_context.get_eex_day_context(data, local_now)
    intervals_all = read_tibber_intervals_for_dates(runtime, {today, tomorrow})
    intervals_all, tomorrow_price_source = market_context.apply_eex_proxy_prices(
        intervals_all,
        eex_days,
        local_now.date(),
        local_now.date() + dt.timedelta(days=1),
    )
    now_floor = floor_to_quarter(local_now)
    now_ts_ms = int(now_ts * 1000)
    intervals = [it for it in intervals_all if it["dt"] >= now_floor]
    intervals = intervals[: int(math.ceil(settings.planning_horizon_h / SLOT_H))]
    if soc is not None:
        start_e = clamp(
            settings.battery_capacity_kwh * soc / 100.0,
            settings.min_energy_kwh,
            settings.max_energy_kwh,
        )
    else:
        start_e = advance_virtual_energy(settings, data, now_ts)

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
                / max(1e-9, actual_today_charge_in_kwh * settings.round_trip_efficiency)
            ) * 100.0
    if actual_inventory_cost_ct_per_kwh is not None:
        inventory_accounting_floor_ct = (
            actual_inventory_cost_ct_per_kwh + settings.min_margin_ct_per_kwh
        )
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
        pv_capacity_kwp=settings.pv_capacity_kwp,
        pv_inverter_kw=settings.pv_inverter_kw,
        history=runtime.forecast_history,
        context=runtime.forecast_context,
        weather=runtime.forecast_weather,
        component_specs=runtime.forecast_component_specs,
    )
    plan = _planning_service(settings).plan(
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
            data,
            int(now_ts * 1000),
            today,
            (start_e / settings.battery_capacity_kwh) * 100.0,
            mode,
            rec_w,
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
        "backtest_mae_pv_24h_kwh": round(evaluation.mean_24h("pv_mae"), 3)
        if bt24
        else None,
        "backtest_mae_load_24h_kwh": round(evaluation.mean_24h("load_mae"), 3)
        if bt24
        else None,
        "backtest_mae_pv_7d_kwh": round(evaluation.mean_7d("pv_mae"), 3)
        if bt7d
        else None,
        "backtest_mae_load_7d_kwh": round(evaluation.mean_7d("load_mae"), 3)
        if bt7d
        else None,
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
        "virtual_soc_start_pct": round(
            (start_e / settings.battery_capacity_kwh) * 100.0, 2
        ),
        "soc_min_pct": round(settings.min_soc_pct, 1),
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
        "profile_48h_pv_actual_power": fetch_pv_actual_profile(runtime, 48),
        "profile_48h_house_actual_power": fetch_house_actual_profile(
            runtime, 48, data["samples"]
        ),
        "profile_48h_grid_net_actual_power": fetch_net_actual_profile(runtime, 48),
        "timestamp": now.isoformat(),
    }

    data["last_output"] = out
    save_state(runtime, data)
    return out

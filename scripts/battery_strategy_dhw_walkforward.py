#!/usr/bin/env python3
"""Walk-forward replay of the DHW forecaster over retained feature history."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.component_config import LoadComponentSpec
from custom_components.battery_strategy.const import LOAD_PROFILE_HEAT_PUMP
from custom_components.battery_strategy.contracts import (
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadDriverSnapshot,
    LoadFeatureValue,
    LoadForecastContext,
    SlotKey,
    WeatherSlot,
)
from custom_components.battery_strategy.feature_store import CompressedFeatureStore
from custom_components.battery_strategy.forecasting.components import _component_power_w
from custom_components.battery_strategy.forecasting.heat_pump import (
    adjust_heat_pump_forecast,
    completed_dhw_cycles,
)
from custom_components.battery_strategy.forecasting.history import ForecastTargetInput

SLOT_MS = 15 * 60 * 1000
MIN_ACTIVE_ENERGY_KWH = 0.02


def _component(item: HistoricalFeatureSlot) -> LoadComponentEnergy | None:
    return next(
        (
            value
            for value in item.load_components
            if value.component_key == "heat_pump_dhw"
        ),
        None,
    )


def _feature(component: LoadComponentEnergy | None, key: str) -> float | None:
    if component is None:
        return None
    value = next((item for item in component.features if item.feature_key == key), None)
    if value is None or value.quality.coverage < 0.999 or value.quality.flags:
        return None
    return value.value


def evaluate(args: argparse.Namespace) -> dict[str, object]:
    """Replay every mature cycle using only evidence available at each cutoff."""
    store = CompressedFeatureStore(args.feature_store)
    store.initialize()
    all_slots = store.load(0, 2**63 - 1)
    timezone = ZoneInfo(args.timezone)
    horizon_slots = round(args.horizon_hours * 4)
    minimum_history_slots = round(args.minimum_history_days * 96)
    actual_by_start = {
        item.slot.start_ms: component.energy_kwh
        for item in all_slots
        if (component := _component(item)) is not None
        and component.quality.coverage >= 0.999
        and not component.quality.flags
    }
    evaluation_as_of_ms = max((item.slot.end_ms for item in all_slots), default=0)
    evaluation_request = ForecastRequest(
        evaluation_as_of_ms,
        args.timezone,
        (SlotKey(evaluation_as_of_ms, evaluation_as_of_ms + SLOT_MS),),
    )
    actual_cycles = completed_dhw_cycles(all_slots, evaluation_request, args.target_c)
    dhw_spec = LoadComponentSpec(
        "heat_pump_dhw",
        LOAD_PROFILE_HEAT_PUMP,
        args.allowed_windows,
    )
    rows = []
    for cycle in actual_cycles:
        actual_start_ms = cycle.start_ms
        for lead_hours in args.lead_hours:
            as_of_ms = actual_start_ms - round(lead_hours * 3_600_000)
            history = tuple(item for item in all_slots if item.slot.end_ms <= as_of_ms)
            if not history or len(history) < minimum_history_slots:
                continue
            current = _component(history[-1])
            temperature_c = _feature(current, "dhw_temperature_c")
            if temperature_c is None or current is None:
                continue
            slots = tuple(
                SlotKey(
                    as_of_ms + index * SLOT_MS,
                    as_of_ms + (index + 1) * SLOT_MS,
                )
                for index in range(horizon_slots)
            )
            request = ForecastRequest(as_of_ms, args.timezone, slots)
            targets = tuple(
                ForecastTargetInput(
                    dt.datetime.fromtimestamp(
                        slot.start_ms / 1000.0, dt.UTC
                    ).astimezone(timezone),
                    1.0,
                )
                for slot in slots
            )
            retained_features = tuple(
                item
                for item in current.features
                if item.feature_key not in {"dhw_target_c", "dhw_differential_c"}
            )
            context = LoadForecastContext(
                0.0,
                (
                    LoadDriverSnapshot(
                        "heat_pump_dhw",
                        0.0,
                        features=(
                            *retained_features,
                            LoadFeatureValue("dhw_target_c", args.target_c),
                            LoadFeatureValue("dhw_differential_c", args.hysteresis_k),
                        ),
                    ),
                    LoadDriverSnapshot("heat_pump_space_heating", 0.0),
                ),
            )
            current_oat_c = _feature(current, "outdoor_temperature_c")
            replay_weather = tuple(
                WeatherSlot(slot, temperature_c=current_oat_c) for slot in slots
            )
            baseline = tuple(
                _component_power_w(
                    dhw_spec,
                    target.local_start,
                    history,
                    context,
                    replay_weather[index],
                    request,
                )
                * 0.25
                / 1000.0
                for index, target in enumerate(targets)
            )
            forecast = adjust_heat_pump_forecast(
                request,
                history,
                targets,
                context,
                args.allowed_windows,
                baseline,
                tuple(0.0 for _ in baseline),
                tuple(current_oat_c for _ in slots),
            ).dhw_kwh
            predicted_start_ms = next(
                (
                    slots[index].start_ms
                    for index, value in enumerate(forecast)
                    if value >= MIN_ACTIVE_ENERGY_KWH
                ),
                None,
            )
            slot_errors = tuple(
                abs(forecast[index] - actual_by_start[slot.start_ms])
                for index, slot in enumerate(slots)
                if slot.start_ms in actual_by_start
            )
            slot_mae = statistics.fmean(slot_errors) if slot_errors else None
            forecast_energy_kwh = first_cycle_energy(forecast)
            actual_energy_kwh = cycle.total_energy_kwh
            rows.append(
                {
                    "actual_start_ms": actual_start_ms,
                    "lead_hours": lead_hours,
                    "predicted_start_ms": predicted_start_ms,
                    "start_error_minutes": (
                        None
                        if predicted_start_ms is None
                        else abs(predicted_start_ms - actual_start_ms) / 60_000.0
                    ),
                    "slot_mae_kwh": slot_mae,
                    "forecast_energy_kwh": forecast_energy_kwh,
                    "actual_energy_kwh": actual_energy_kwh,
                    "energy_error_kwh": forecast_energy_kwh - actual_energy_kwh,
                }
            )

    summary = {}
    for lead_hours in args.lead_hours:
        selected = [item for item in rows if item["lead_hours"] == lead_hours]
        start_errors = [
            item["start_error_minutes"]
            for item in selected
            if item["start_error_minutes"] is not None
        ]
        energy_errors = [
            item["energy_error_kwh"]
            for item in selected
            if item["predicted_start_ms"] is not None
        ]
        slot_errors = [
            item["slot_mae_kwh"]
            for item in selected
            if item["slot_mae_kwh"] is not None
        ]
        summary[str(lead_hours)] = {
            "cycles": len(selected),
            "missed_cycles": sum(
                item["predicted_start_ms"] is None for item in selected
            ),
            "start_mae_minutes": (
                statistics.fmean(start_errors) if start_errors else None
            ),
            "slot_mae_kwh": (statistics.fmean(slot_errors) if slot_errors else None),
            "energy_mae_kwh": (
                statistics.fmean(abs(value) for value in energy_errors)
                if energy_errors
                else None
            ),
            "energy_bias_kwh": (
                statistics.fmean(energy_errors) if energy_errors else None
            ),
        }
    return {
        "label": args.label,
        "configuration": {
            "timezone": args.timezone,
            "target_c": args.target_c,
            "hysteresis_k": args.hysteresis_k,
            "allowed_windows": args.allowed_windows,
            "lead_hours": args.lead_hours,
            "horizon_hours": args.horizon_hours,
            "minimum_history_days": args.minimum_history_days,
        },
        "summary": summary,
        "rows": rows,
    }


def first_cycle_energy(forecast: tuple[float, ...]) -> float:
    """Return one contiguous forecast event, excluding later cycles."""
    start = next(
        (
            index
            for index, value in enumerate(forecast)
            if value >= MIN_ACTIVE_ENERGY_KWH
        ),
        None,
    )
    if start is None:
        return 0.0
    total = 0.0
    for value in forecast[start:]:
        if value < MIN_ACTIVE_ENERGY_KWH:
            break
        total += value
    return total


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feature_store", type=Path)
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--target-c", required=True, type=float)
    parser.add_argument("--hysteresis-k", required=True, type=float)
    parser.add_argument("--allowed-windows", required=True)
    parser.add_argument("--lead-hours", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument("--horizon-hours", type=int, default=8)
    parser.add_argument("--minimum-history-days", type=int, default=7)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(evaluate(_arguments()), sort_keys=True))

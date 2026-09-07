#!/usr/bin/env python3
"""Walk-forward replay of the DHW forecaster over retained feature history."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.component_config import time_allowed
from custom_components.battery_strategy.contracts import (
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadDriverSnapshot,
    LoadFeatureValue,
    LoadForecastContext,
    SlotKey,
)
from custom_components.battery_strategy.feature_store import CompressedFeatureStore
from custom_components.battery_strategy.forecasting.heat_pump import (
    adjust_heat_pump_forecast,
)
from custom_components.battery_strategy.forecasting.history import ForecastTargetInput

SLOT_MS = 15 * 60 * 1000
MIN_ACTIVE_ENERGY_KWH = 0.02
MIN_ACTIVE_FRACTION = 0.05


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


def _active(item: HistoricalFeatureSlot) -> bool:
    component = _component(item)
    if (
        component is None
        or component.quality.coverage < 0.999
        or component.quality.flags
    ):
        return False
    charging = _feature(component, "dhw_charging_fraction")
    return component.energy_kwh >= MIN_ACTIVE_ENERGY_KWH or (
        charging is not None and charging >= MIN_ACTIVE_FRACTION
    )


def _cycle_groups(
    history: tuple[HistoricalFeatureSlot, ...],
) -> list[list[HistoricalFeatureSlot]]:
    groups: list[list[HistoricalFeatureSlot]] = []
    current: list[HistoricalFeatureSlot] = []
    for item in history:
        if _active(item):
            if current and current[-1].slot.end_ms != item.slot.start_ms:
                groups.append(current)
                current = []
            current.append(item)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _baseline_energy(
    history: tuple[HistoricalFeatureSlot, ...],
    local_start: dt.datetime,
) -> float:
    target_slot = local_start.hour * 4 + local_start.minute // 15
    target_weekend = local_start.weekday() >= 5
    values = []
    for item in history:
        component = _component(item)
        if (
            component is None
            or component.quality.coverage < 0.999
            or component.quality.flags
        ):
            continue
        local = dt.datetime.fromtimestamp(
            item.slot.start_ms / 1000.0, dt.UTC
        ).astimezone(local_start.tzinfo)
        if (
            local.hour * 4 + local.minute // 15 == target_slot
            and (local.weekday() >= 5) == target_weekend
        ):
            values.append(component.energy_kwh)
    return float(statistics.median(values[-12:])) if values else 0.0


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
    }
    rows = []
    for group in _cycle_groups(all_slots):
        actual_start_ms = group[0].slot.start_ms
        for lead_hours in args.lead_hours:
            as_of_ms = actual_start_ms - round(lead_hours * 3_600_000)
            history = tuple(item for item in all_slots if item.slot.end_ms <= as_of_ms)
            if len(history) < minimum_history_slots:
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
            baseline = tuple(
                _baseline_energy(history, target.local_start)
                if time_allowed(target.local_start, args.allowed_windows)
                else 0.0
                for target in targets
            )
            forecast = adjust_heat_pump_forecast(
                request,
                history,
                targets,
                context,
                args.allowed_windows,
                baseline,
                tuple(0.0 for _ in baseline),
            ).dhw_kwh
            predicted_start_ms = next(
                (
                    slots[index].start_ms
                    for index, value in enumerate(forecast)
                    if value >= MIN_ACTIVE_ENERGY_KWH
                ),
                None,
            )
            slot_mae = statistics.fmean(
                abs(forecast[index] - actual_by_start.get(slot.start_ms, 0.0))
                for index, slot in enumerate(slots)
            )
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
        summary[str(lead_hours)] = {
            "cycles": len(selected),
            "missed_cycles": sum(
                item["predicted_start_ms"] is None for item in selected
            ),
            "start_mae_minutes": (
                statistics.fmean(start_errors) if start_errors else None
            ),
            "slot_mae_kwh": (
                statistics.fmean(item["slot_mae_kwh"] for item in selected)
                if selected
                else None
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

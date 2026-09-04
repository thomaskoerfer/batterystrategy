"""Measured battery savings accounting, independent from plan generation."""

from __future__ import annotations

import bisect
import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .planning_runtime import HistoryRole
from .runtime_market_data import TariffInterval

if TYPE_CHECKING:
    from .planning_state import SavingsState

Series = list[tuple[float, float]]
HistoryReader = Callable[[Iterable[HistoryRole], float], dict[HistoryRole, Series]]
PriceReader = Callable[[set[str]], Iterable[TariffInterval]]


@dataclass(frozen=True)
class SavingsEntities:
    """Normalized runtime roles required by measured-savings accounting."""

    price: HistoryRole
    battery_input_energy: HistoryRole
    battery_output_energy: HistoryRole
    grid_import: HistoryRole
    grid_export: HistoryRole
    battery_charge_power: HistoryRole
    battery_discharge_power: HistoryRole


@dataclass(frozen=True)
class SavingsConfig:
    """Retention and time semantics for the measured ledger."""

    timezone: dt.tzinfo
    retention_days: int
    entities: SavingsEntities


class SavingsLedger:
    """Update persistent actual savings from measured energy counters."""

    def __init__(
        self,
        *,
        config: SavingsConfig,
        history_reader: HistoryReader,
        price_reader: PriceReader,
    ) -> None:
        self._config = config
        self._history_reader = history_reader
        self._price_reader = price_reader

    @staticmethod
    def _series_index(series: Series) -> tuple[list[float], list[float]]:
        return (
            [float(timestamp) for timestamp, _ in series],
            [float(value) for _, value in series],
        )

    @staticmethod
    def _value_at_or_before(
        index: tuple[list[float], list[float]], timestamp: float
    ) -> float | None:
        timestamps, values = index
        if not timestamps:
            return None
        position = bisect.bisect_right(timestamps, float(timestamp)) - 1
        return values[position] if position >= 0 else None

    def _local_datetime(self, timestamp: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(float(timestamp), dt.timezone.utc).astimezone(
            self._config.timezone
        )

    def _local_dates_between(self, start: float, end: float) -> set[str]:
        start_day = self._local_datetime(start).date()
        end_day = self._local_datetime(end).date()
        dates = set()
        current = start_day
        while current <= end_day:
            dates.add(current.isoformat())
            current += dt.timedelta(days=1)
        return dates

    def _price_index(self, dates: set[str]) -> tuple[list[float], list[float]]:
        pairs = sorted(
            (
                float(interval.starts_at.timestamp()),
                float(interval.price_eur_per_kwh),
            )
            for interval in self._price_reader(dates)
        )
        return (
            [timestamp for timestamp, _ in pairs],
            [value for _, value in pairs],
        )

    @staticmethod
    def _new_day_record() -> dict[str, float]:
        return {
            "charge_grid_kwh": 0.0,
            "charge_pv_kwh": 0.0,
            "discharge_used_kwh": 0.0,
            "charge_cost_eur": 0.0,
            "discharge_credit_eur": 0.0,
            "saving_eur": 0.0,
        }

    def update(self, state: SavingsState, now_ts: float) -> tuple[dict, float, float]:
        """Stamp new measured charge/discharge deltas at contemporaneous prices."""
        local_now = self._local_datetime(now_ts)
        today = local_now.date().isoformat()
        state.tracker_was_persisted = True
        state.archived_was_persisted = True
        tracker = state.tracker
        daily = state.actual_daily
        archived = float(state.archived_eur)
        last_ts = float(tracker.get("last_ts", 0.0))
        last_input_kwh = tracker.get("last_input_kwh")
        last_output_kwh = tracker.get("last_output_kwh")
        first_run = last_ts == 0.0
        needs_backfill = first_run is False and not tracker.get(
            "savings_backfill_v1_done"
        )
        midnight = dt.datetime.combine(
            local_now.date(), dt.time.min, tzinfo=self._config.timezone
        ).timestamp()
        if first_run:
            query_from = now_ts - 86400.0
        elif needs_backfill:
            query_from = (
                dt.datetime.combine(
                    local_now.date(), dt.time.min, tzinfo=self._config.timezone
                )
                - dt.timedelta(days=2)
            ).timestamp()
        else:
            query_from = min(last_ts - 120.0, midnight)

        entities = self._config.entities
        series_map = self._history_reader(
            (
                entities.price,
                entities.battery_input_energy,
                entities.battery_output_energy,
                entities.grid_import,
                entities.grid_export,
                entities.battery_charge_power,
                entities.battery_discharge_power,
            ),
            query_from,
        )
        price_series = series_map.get(entities.price, [])
        input_series = series_map.get(entities.battery_input_energy, [])
        output_series = series_map.get(entities.battery_output_energy, [])
        grid_import_series = series_map.get(entities.grid_import, [])
        grid_export_series = series_map.get(entities.grid_export, [])
        battery_charge_series = series_map.get(entities.battery_charge_power, [])
        battery_discharge_series = series_map.get(
            entities.battery_discharge_power, []
        )
        tariff_index = self._price_index(self._local_dates_between(query_from, now_ts))

        if first_run:
            tracker["last_input_kwh"] = (
                float(input_series[-1][1]) if input_series else None
            )
            tracker["last_output_kwh"] = (
                float(output_series[-1][1]) if output_series else None
            )
            tracker["last_ts"] = now_ts
        elif tariff_index[0] or price_series:
            fallback_price_index = self._series_index(price_series)
            grid_import_index = self._series_index(grid_import_series)
            grid_export_index = self._series_index(grid_export_series)
            battery_charge_index = self._series_index(battery_charge_series)
            battery_discharge_index = self._series_index(battery_discharge_series)
            if needs_backfill:
                backfill_days = {
                    today,
                    (local_now.date() - dt.timedelta(days=1)).isoformat(),
                }
                for day in backfill_days:
                    daily.pop(day, None)
                input_baseline = float(input_series[0][1]) if input_series else None
                output_baseline = float(output_series[0][1]) if output_series else None
                tracker["savings_backfill_v1_done"] = True
            else:
                backfill_days = None
                input_baseline = float(input_series[0][1]) if input_series else None
                output_baseline = float(output_series[0][1]) if output_series else None
                daily.pop(today, None)
            cutoff = 0.0 if needs_backfill else last_ts

            def price_at(timestamp: float) -> float | None:
                value = self._value_at_or_before(tariff_index, timestamp)
                if value is not None:
                    return value
                value = self._value_at_or_before(fallback_price_index, timestamp)
                if value is None:
                    return None
                return float(value)

            def charge_split(delta_kwh: float, timestamp: float) -> tuple[float, float]:
                grid_import = max(
                    0.0,
                    float(
                        self._value_at_or_before(grid_import_index, timestamp) or 0.0
                    ),
                )
                grid_export = max(
                    0.0,
                    float(
                        self._value_at_or_before(grid_export_index, timestamp) or 0.0
                    ),
                )
                charge_w = max(
                    0.0,
                    float(
                        self._value_at_or_before(battery_charge_index, timestamp)
                        or 0.0
                    ),
                )
                discharge_w = max(
                    0.0,
                    float(
                        self._value_at_or_before(
                            battery_discharge_index, timestamp
                        )
                        or 0.0
                    ),
                )
                battery_power = discharge_w - charge_w
                export_without_battery_w = max(
                    0.0, -(grid_import - grid_export + battery_power)
                )
                pv_ratio = (
                    (1.0 if export_without_battery_w > 1.0 else 0.0)
                    if charge_w <= 1.0
                    else min(1.0, export_without_battery_w / charge_w)
                )
                pv_kwh = delta_kwh * pv_ratio
                return max(0.0, delta_kwh - pv_kwh), pv_kwh

            def process_counter(
                series: Series,
                baseline: float | None,
                previous_tracker_value: float | None,
                *,
                charge: bool,
            ) -> None:
                previous = baseline
                if previous is None and previous_tracker_value is not None:
                    previous = float(previous_tracker_value)
                if previous is None and series:
                    previous = float(series[0][1])
                for timestamp, value in series if previous is not None else []:
                    timestamp = float(timestamp)
                    value = float(value)
                    delta = value - previous
                    previous = value
                    day = self._local_datetime(timestamp).date().isoformat()
                    if timestamp <= cutoff and day != today:
                        continue
                    if delta <= 0.0 or delta > 8.0:
                        continue
                    if backfill_days is not None and day not in backfill_days:
                        continue
                    price = price_at(timestamp)
                    if price is None:
                        continue
                    record = daily.setdefault(day, self._new_day_record())
                    if charge:
                        grid_kwh, pv_kwh = charge_split(delta, timestamp)
                        record["charge_grid_kwh"] += grid_kwh
                        record["charge_pv_kwh"] += pv_kwh
                        record["charge_cost_eur"] += grid_kwh * price
                        record["saving_eur"] -= grid_kwh * price
                    else:
                        # Gross discharge credit is an explicit product decision.
                        record["discharge_used_kwh"] += delta
                        record["discharge_credit_eur"] += delta * price
                        record["saving_eur"] += delta * price

            process_counter(
                input_series,
                input_baseline,
                last_input_kwh,
                charge=True,
            )
            process_counter(
                output_series,
                output_baseline,
                last_output_kwh,
                charge=False,
            )
            if input_series:
                tracker["last_input_kwh"] = float(input_series[-1][1])
            if output_series:
                tracker["last_output_kwh"] = float(output_series[-1][1])
            tracker["last_ts"] = now_ts
        # Without any price source the tracker intentionally stays unchanged.

        keys = (
            "charge_grid_kwh",
            "charge_pv_kwh",
            "discharge_used_kwh",
            "charge_cost_eur",
            "discharge_credit_eur",
            "saving_eur",
        )
        for record in daily.values():
            for key in keys:
                record[key] = round(float(record.get(key, 0.0)), 4)
        trim_before = (
            local_now.date() - dt.timedelta(days=self._config.retention_days)
        ).isoformat()
        for day in sorted(key for key in list(daily) if key < trim_before):
            archived += float(daily.pop(day, {}).get("saving_eur", 0.0))
        state.archived_eur = round(archived, 4)
        today_saving = float(daily.get(today, {}).get("saving_eur", 0.0))
        lifetime = archived + sum(
            float(record.get("saving_eur", 0.0)) for record in daily.values()
        )
        return daily, round(today_saving, 3), round(lifetime, 3)

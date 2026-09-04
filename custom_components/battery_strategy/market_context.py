"""Market enrichment and commercial planning context.

This module owns price normalization, optional EEX enrichment and the
commercial horizon metadata consumed by planning. It does not forecast load or
PV and it never invokes the optimizer.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import statistics
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from .planning_state import MarketState


@dataclass(frozen=True)
class MarketContextConfig:
    """Runtime configuration for market enrichment and price policy."""

    timezone: dt.tzinfo
    round_trip_efficiency: float
    min_margin_ct_per_kwh: float
    terminal_rank_threshold: float = 0.35
    terminal_value_cap_ct: float = 25.0
    slots_per_day: int = 96
    eex_cache_ttl_s: int = 6 * 3600
    proxy_min_full_day_slots: int = 90
    proxy_recent_days: int = 5
    proxy_min_retail_markup_ct: float = 18.0
    proxy_max_base_retail_markup_ct: float = 28.0
    proxy_max_peak_retail_markup_ct: float = 32.0
    proxy_min_price_ct: float = 12.0
    proxy_max_price_ct: float = 70.0


class MarketContextService:
    """Build provider-neutral market context for one planning snapshot."""

    _EEX_SCOPE_URL = (
        "https://api.eex-group.com/pub/customise-widget/filter-data-with-scope"
    )
    _EEX_TABLE_URL = "https://api.eex-group.com/pub/market-data/table-data"

    def __init__(self, config: MarketContextConfig) -> None:
        self._config = config

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    @staticmethod
    def _quantile(sorted_values: list[float], quantile: float) -> float | None:
        if not sorted_values:
            return None
        if len(sorted_values) == 1:
            return float(sorted_values[0])
        position = (len(sorted_values) - 1) * quantile
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return float(sorted_values[lower])
        fraction = position - lower
        return float(
            sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction
        )

    def _local_datetime(self, timestamp: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(float(timestamp), dt.timezone.utc).astimezone(
            self._config.timezone
        )

    @staticmethod
    def _slot_index(value: dt.datetime) -> int:
        return value.hour * 4 + value.minute // 15

    @staticmethod
    def _eex_headers() -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.eex.com/",
            "Origin": "https://www.eex.com",
            "Accept": "application/json, text/plain, */*",
        }

    @staticmethod
    def _fetch_json(
        url: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        request = Request(url, data=data, headers=headers or {})
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed EEX URLs
            return json.loads(response.read().decode("utf-8"))

    def _eex_filter_rows(self, product: str) -> list[dict]:
        payload = [
            {
                "commodity": "POWER",
                "pricing": "F",
                "area": "DE",
                "product": product,
                "productSpecific": "All",
                "maturityType": "Day",
            }
        ]
        encoded = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        query = urllib.parse.urlencode({"data": encoded})
        headers = self._eex_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        response = self._fetch_json(
            f"{self._EEX_SCOPE_URL}?{query}",
            data=query.encode("utf-8"),
            headers=headers,
        )
        rows = []
        for row in response.get("data", []):
            record = dict(zip(response.get("header", []), row, strict=False))
            year = record.get("displayYear")
            month = record.get("displayMonth")
            day = record.get("displayDay")
            shortcode = record.get("shortCode")
            maturity = record.get("maturity")
            if year and month and day and shortcode and maturity:
                rows.append(
                    {
                        "delivery_date": (
                            f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                        ),
                        "shortCode": shortcode,
                        "maturity": str(maturity),
                        "product": product,
                    }
                )
        return rows

    def _eex_fetch_settlement(self, row: dict, trade_date: str) -> dict | None:
        params = {
            "shortCode": row["shortCode"],
            "commodity": "POWER",
            "pricing": "F",
            "area": "DE",
            "product": row["product"],
            "maturity": row["maturity"],
            "startDate": trade_date,
            "endDate": trade_date,
            "maturityType": "Day",
            "isRolling": "true",
        }
        response = self._fetch_json(
            f"{self._EEX_TABLE_URL}?{urllib.parse.urlencode(params)}",
            headers=self._eex_headers(),
        )
        for data_row in response.get("data", []):
            record = dict(zip(response.get("header", []), data_row, strict=False))
            price = record.get("settlPx")
            if price is not None:
                return {
                    "trade_date": record.get("tradeDate", trade_date),
                    "settl_eur_mwh": float(price),
                    "settl_ct_kwh": round(float(price) / 10.0, 3),
                }
        return None

    def get_eex_day_context(
        self, state: MarketState, local_now: dt.datetime
    ) -> dict[str, dict]:
        """Return cached EEX day products, refreshing them when needed."""
        cache = state.eex_cache
        fetched_at = float(cache.get("fetched_at_ts", 0.0) or 0.0)
        if (
            cache.get("days")
            and (local_now.timestamp() - fetched_at) < self._config.eex_cache_ttl_s
        ):
            return cache["days"]

        target_dates = [
            (local_now.date() + dt.timedelta(days=offset)).isoformat()
            for offset in range(4)
        ]
        result: dict[str, dict] = {day: {} for day in target_dates}
        try:
            base_rows = {
                row["delivery_date"]: row for row in self._eex_filter_rows("Base")
            }
            peak_rows = {
                row["delivery_date"]: row for row in self._eex_filter_rows("Peak")
            }
            trade_dates = [
                (local_now.date() - dt.timedelta(days=offset)).isoformat()
                for offset in range(1, 8)
            ]
            for delivery_date in target_dates:
                for product, rows in (("base", base_rows), ("peak", peak_rows)):
                    row = rows.get(delivery_date)
                    if not row:
                        continue
                    settlement = None
                    for trade_date in trade_dates:
                        settlement = self._eex_fetch_settlement(row, trade_date)
                        if settlement:
                            break
                    if settlement:
                        result[delivery_date][product] = settlement
            for record in result.values():
                if record.get("base") and record.get("peak"):
                    record["spread_ct_kwh"] = round(
                        record["peak"]["settl_ct_kwh"] - record["base"]["settl_ct_kwh"],
                        3,
                    )
        except Exception:  # noqa: BLE001 - EEX is optional diagnostic context.
            pass

        cache["fetched_at_ts"] = local_now.timestamp()
        cache["days"] = result
        return result

    def compute_price_quantiles(
        self,
        samples: list[dict],
        local_now: dt.datetime,
        current_price_ct: float | None,
        tomorrow_prices: list,
    ) -> dict:
        """Compare current and next-day prices with retained weekday history."""
        slot = self._slot_index(local_now)
        weekday = local_now.weekday()
        values = sorted(
            float(sample["price_ct"])
            for sample in samples
            if sample.get("ts") is not None
            and sample.get("price_ct") not in (None, 0)
            and self._local_datetime(sample["ts"]).weekday() == weekday
            and self._slot_index(self._local_datetime(sample["ts"])) == slot
        )
        median = self._quantile(values, 0.5)
        q20 = self._quantile(values, 0.2)
        q80 = self._quantile(values, 0.8)
        rank = None
        if values and current_price_ct is not None:
            rank = sum(value <= current_price_ct for value in values) / len(values)

        tomorrow_values = (
            sorted(value for _, value in tomorrow_prices) if tomorrow_prices else []
        )
        tomorrow_min = min(tomorrow_values) if tomorrow_values else None
        tomorrow_rank = None
        if tomorrow_prices:
            tomorrow_weekday = (local_now.date() + dt.timedelta(days=1)).weekday()
            weekday_values = sorted(
                float(sample["price_ct"])
                for sample in samples
                if sample.get("ts") is not None
                and sample.get("price_ct") not in (None, 0)
                and self._local_datetime(sample["ts"]).weekday() == tomorrow_weekday
            )
            if weekday_values and tomorrow_min is not None:
                tomorrow_rank = sum(
                    value <= tomorrow_min for value in weekday_values
                ) / len(weekday_values)

        return {
            "current_slot_median_ct": round(median, 3) if median is not None else None,
            "current_slot_q20_ct": round(q20, 3) if q20 is not None else None,
            "current_slot_q80_ct": round(q80, 3) if q80 is not None else None,
            "current_slot_rank": round(rank, 3) if rank is not None else None,
            "tomorrow_min_price_ct": (
                round(tomorrow_min, 3) if tomorrow_min is not None else None
            ),
            "tomorrow_min_rank": (
                round(tomorrow_rank, 3) if tomorrow_rank is not None else None
            ),
        }

    def _weekday_price_rank(
        self, samples: list[dict], target_date: dt.date, price_value: float | None
    ) -> float | None:
        if price_value is None:
            return None
        values = sorted(
            float(sample["price_ct"])
            for sample in samples
            if sample.get("ts") is not None
            and sample.get("price_ct") not in (None, 0)
            and self._local_datetime(sample["ts"]).date().weekday()
            == target_date.weekday()
        )
        if not values:
            return None
        return sum(value <= price_value for value in values) / len(values)

    def _weekday_price_quantile(
        self, samples: list[dict], target_date: dt.date, quantile: float
    ) -> float | None:
        values = sorted(
            float(sample["price_ct"])
            for sample in samples
            if sample.get("ts") is not None
            and sample.get("price_ct") not in (None, 0)
            and self._local_datetime(sample["ts"]).date().weekday()
            == target_date.weekday()
        )
        return self._quantile(values, quantile)

    @staticmethod
    def _eex_settlement_ct(eex_day: dict, product: str) -> float | None:
        try:
            value = eex_day.get(product, {}).get("settl_ct_kwh")
            return None if value is None else float(value)
        except (AttributeError, TypeError, ValueError):
            return None

    def _intervals_by_date(self, intervals: list[dict]) -> dict:
        by_date: dict = {}
        for interval in intervals:
            if interval.get("source", "tibber") != "tibber":
                continue
            by_date.setdefault(interval["dt"].date(), []).append(interval)
        return {
            day: sorted(items, key=lambda item: item["dt"])
            for day, items in by_date.items()
        }

    def _recent_slot_offsets(self, by_date: dict, recent_days: list) -> dict:
        slot_values = {slot: [] for slot in range(self._config.slots_per_day)}
        for day in recent_days:
            items = by_date.get(day, [])
            prices = [float(item["price_eur"]) * 100.0 for item in items]
            if not prices:
                continue
            average = statistics.mean(prices)
            for item in items:
                slot_values[self._slot_index(item["dt"])].append(
                    float(item["price_eur"]) * 100.0 - average
                )
        return {
            slot: statistics.median(values)
            for slot, values in slot_values.items()
            if values
        }

    @staticmethod
    def _is_peak_slot(slot: int) -> bool:
        return 8 <= int(slot) // 4 < 20

    def _retail_markups(
        self, by_date: dict, eex_days: dict, reference_date: dt.date
    ) -> tuple[float, float]:
        reference_items = by_date.get(reference_date, [])
        reference_eex = (eex_days or {}).get(reference_date.isoformat(), {})
        base_ct = self._eex_settlement_ct(reference_eex, "base")
        peak_ct = self._eex_settlement_ct(reference_eex, "peak")
        if (
            len(reference_items) < self._config.proxy_min_full_day_slots
            or base_ct is None
        ):
            return 20.0, 20.0
        prices = [float(item["price_eur"]) * 100.0 for item in reference_items]
        peak_prices = [
            float(item["price_eur"]) * 100.0
            for item in reference_items
            if self._is_peak_slot(self._slot_index(item["dt"]))
        ]
        base_markup = statistics.mean(prices) - base_ct
        peak_markup = (
            statistics.mean(peak_prices) - peak_ct
            if peak_prices and peak_ct is not None
            else base_markup
        )
        return (
            self._clamp(
                base_markup,
                self._config.proxy_min_retail_markup_ct,
                self._config.proxy_max_base_retail_markup_ct,
            ),
            self._clamp(
                peak_markup,
                self._config.proxy_min_retail_markup_ct,
                self._config.proxy_max_peak_retail_markup_ct,
            ),
        )

    def build_eex_proxy_day_prices(
        self,
        tibber_intervals: list[dict],
        eex_days: dict,
        reference_date: dt.date,
        target_date: dt.date,
    ) -> list[dict]:
        """Build a slot-aligned retail-price proxy from EEX and recent shape."""
        target_context = (eex_days or {}).get(target_date.isoformat(), {})
        base_ct = self._eex_settlement_ct(target_context, "base")
        peak_ct = self._eex_settlement_ct(target_context, "peak")
        if base_ct is None:
            return []
        if peak_ct is None:
            peak_ct = base_ct

        by_date = self._intervals_by_date(tibber_intervals)
        recent_days = [
            day
            for day in sorted(by_date)
            if day < target_date
            and len(by_date.get(day, [])) >= self._config.proxy_min_full_day_slots
        ][-self._config.proxy_recent_days :]
        slot_offsets = self._recent_slot_offsets(by_date, recent_days)
        base_markup, peak_markup = self._retail_markups(
            by_date, eex_days, reference_date
        )
        day_average_ct = base_ct + base_markup
        peak_average_ct = peak_ct + peak_markup
        raw = [
            day_average_ct + slot_offsets.get(slot, 0.0)
            for slot in range(self._config.slots_per_day)
        ]
        peak_slots = [
            slot
            for slot in range(self._config.slots_per_day)
            if self._is_peak_slot(slot)
        ]
        off_peak_slots = [
            slot for slot in range(self._config.slots_per_day) if slot not in peak_slots
        ]
        raw_peak_average = statistics.mean(raw[slot] for slot in peak_slots)
        raw_off_peak_average = statistics.mean(raw[slot] for slot in off_peak_slots)
        desired_off_peak_average = (
            day_average_ct * self._config.slots_per_day
            - peak_average_ct * len(peak_slots)
        ) / len(off_peak_slots)

        local_midnight = dt.datetime.combine(
            target_date, dt.time.min, tzinfo=self._config.timezone
        )
        result = []
        for slot, price_ct in enumerate(raw):
            if self._is_peak_slot(slot):
                price_ct += peak_average_ct - raw_peak_average
            else:
                price_ct += desired_off_peak_average - raw_off_peak_average
            price_ct = self._clamp(
                price_ct,
                self._config.proxy_min_price_ct,
                self._config.proxy_max_price_ct,
            )
            slot_datetime = local_midnight + dt.timedelta(minutes=15 * slot)
            result.append(
                {
                    "dt": slot_datetime,
                    "ts": slot_datetime.isoformat(),
                    "price_eur": round(price_ct / 100.0, 5),
                    "source": "eex_proxy",
                }
            )
        return result

    def apply_eex_proxy_prices(
        self,
        intervals: list[dict],
        eex_days: dict,
        today: dt.date,
        tomorrow: dt.date,
    ) -> tuple[list[dict], str]:
        """Fill a missing next tariff day without replacing real prices."""
        existing = list(intervals)
        real_tomorrow = [
            interval
            for interval in existing
            if interval["dt"].date() == tomorrow
            and interval.get("source", "tibber") == "tibber"
        ]
        if len(real_tomorrow) >= self._config.proxy_min_full_day_slots:
            return sorted(existing, key=lambda item: item["dt"]), "tibber"
        proxy = self.build_eex_proxy_day_prices(existing, eex_days, today, tomorrow)
        if not proxy:
            return sorted(existing, key=lambda item: item["dt"]), "missing"
        retained = [
            interval for interval in existing if interval["dt"].date() != tomorrow
        ]
        return sorted(retained + proxy, key=lambda item: item["dt"]), "eex_proxy"

    def build_plan_metadata(
        self,
        intervals: list[dict],
        samples: list[dict],
        *,
        eex_days: dict | None = None,
        forecast_diagnostics: dict | None = None,
    ) -> dict:
        """Build commercial policy inputs and publication metadata."""
        if not intervals:
            return {
                "today": {},
                "tomorrow": {},
                "price_stats": {},
                "forecast_diagnostics": dict(forecast_diagnostics or {}),
            }
        prices_ct = [float(item["price_eur"]) * 100.0 for item in intervals]
        sorted_prices = sorted(prices_ct)
        p_low = sorted_prices[int(0.3 * (len(sorted_prices) - 1))]
        p_high = sorted_prices[int(0.7 * (len(sorted_prices) - 1))]
        first_date = intervals[0]["dt"].date()
        today = first_date.isoformat()
        tomorrow_date = first_date + dt.timedelta(days=1)
        tomorrow = tomorrow_date.isoformat()
        slots = [
            {
                "date": item["dt"].date().isoformat(),
                "price_ct": float(item["price_eur"]) * 100.0,
                "weekday_rank": self._weekday_price_rank(
                    samples,
                    item["dt"].date(),
                    float(item["price_eur"]) * 100.0,
                ),
            }
            for item in intervals
        ]
        tomorrow_prices = [
            slot["price_ct"] for slot in slots if slot["date"] == tomorrow
        ]
        tomorrow_min_ct = min(tomorrow_prices) if tomorrow_prices else None
        tomorrow_min_rank = self._weekday_price_rank(
            samples, tomorrow_date, tomorrow_min_ct
        )
        terminal_value_ct = 0.0
        discharge_floor_ct = None
        cheap_anchor_ct = None
        cheap_anchor_rank = None
        if (
            tomorrow_min_ct is not None
            and tomorrow_min_rank is not None
            and tomorrow_min_rank <= self._config.terminal_rank_threshold
        ):
            cheapness = self._clamp(
                (self._config.terminal_rank_threshold - tomorrow_min_rank)
                / self._config.terminal_rank_threshold,
                0.0,
                1.0,
            )
            terminal_value_ct = self._clamp(
                max(0.0, p_high - tomorrow_min_ct) * (0.5 + cheapness),
                0.0,
                self._config.terminal_value_cap_ct,
            )
            discharge_floor_ct = (
                tomorrow_min_ct / self._config.round_trip_efficiency
            ) + self._config.min_margin_ct_per_kwh
            cheap_anchor_ct = tomorrow_min_ct
            cheap_anchor_rank = tomorrow_min_rank

        horizon_min_slot = min(slots, key=lambda slot: slot["price_ct"])
        horizon_min_ct = float(horizon_min_slot["price_ct"])
        horizon_min_rank = horizon_min_slot.get("weekday_rank")
        if horizon_min_rank is not None:
            cheapness = self._clamp((0.5 - horizon_min_rank) / 0.5, 0.0, 1.0)
            if cheapness > 0.0:
                tail_date = intervals[-1]["dt"].date() + dt.timedelta(days=1)
                tail_reference_ct = (
                    self._weekday_price_quantile(samples, tail_date, 0.8) or p_high
                )
                tail_context = (eex_days or {}).get(tail_date.isoformat(), {})
                tail_eex_values = [
                    tail_context.get("base", {}).get("settl_ct_kwh"),
                    tail_context.get("peak", {}).get("settl_ct_kwh"),
                ]
                tail_eex_values = [
                    float(value) for value in tail_eex_values if value is not None
                ]
                if tail_eex_values:
                    tail_reference_ct = max([tail_reference_ct, *tail_eex_values])
                terminal_value_ct = max(
                    terminal_value_ct,
                    self._clamp(
                        (max(p_high, tail_reference_ct) - horizon_min_ct) * cheapness,
                        0.0,
                        self._config.terminal_value_cap_ct,
                    ),
                )
                inferred_floor_ct = (
                    horizon_min_ct / self._config.round_trip_efficiency
                ) + self._config.min_margin_ct_per_kwh
                if discharge_floor_ct is None or inferred_floor_ct < discharge_floor_ct:
                    discharge_floor_ct = inferred_floor_ct
                if cheap_anchor_ct is None or horizon_min_ct < cheap_anchor_ct:
                    cheap_anchor_ct = horizon_min_ct
                    cheap_anchor_rank = horizon_min_rank

        return {
            "today": {"date": today, "saving_eur": 0.0},
            "tomorrow": {"date": tomorrow, "saving_eur": 0.0},
            "price_stats": {
                "p_low": round(p_low, 2),
                "p_high": round(p_high, 2),
                "avg": round(sum(prices_ct) / len(prices_ct), 2),
                "min": round(min(prices_ct), 2),
                "max": round(max(prices_ct), 2),
                "tomorrow_min_rank": (
                    round(tomorrow_min_rank, 3)
                    if tomorrow_min_rank is not None
                    else None
                ),
                "terminal_value_ct": round(terminal_value_ct, 3),
                "discharge_floor_ct": (
                    round(discharge_floor_ct, 3)
                    if discharge_floor_ct is not None
                    else None
                ),
                "cheap_anchor_ct": (
                    round(cheap_anchor_ct, 3) if cheap_anchor_ct is not None else None
                ),
                "cheap_anchor_rank": (
                    round(cheap_anchor_rank, 3)
                    if cheap_anchor_rank is not None
                    else None
                ),
            },
            "forecast_diagnostics": dict(forecast_diagnostics or {}),
        }

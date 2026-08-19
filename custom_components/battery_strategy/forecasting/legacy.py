"""Extracted implementation of the proven legacy forecast mathematics.

The legacy sample shape remains transitional while the finalized 15-minute
feature store is introduced. Outputs already use the target forecast contracts.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..contracts import (
    ForecastBundle,
    ForecastRequest,
    ForecastSlot,
    LoadForecast,
    LoadForecastContext,
    PvForecast,
    QuantileEnergy,
)

SLOT_H = 0.25
PV_NOWCAST_BLEND_HOURS = 2.5


@dataclass(frozen=True, slots=True)
class LegacyForecastSample:
    """Immutable view of one sample used by the current forecast model."""

    ts_s: float
    load_w: float
    pv_w: float
    grid_import_w: float
    grid_export_w: float

    @classmethod
    def from_mapping(cls, sample: dict) -> LegacyForecastSample:
        """Normalize the fields consumed by the legacy forecast."""
        return cls(
            ts_s=float(sample.get("ts", 0.0) or 0.0),
            load_w=float(sample.get("load_w", 0.0) or 0.0),
            pv_w=max(0.0, float(sample.get("pv_w", 0.0) or 0.0)),
            grid_import_w=float(sample.get("grid_import_w", 0.0) or 0.0),
            grid_export_w=float(sample.get("grid_export_w", 0.0) or 0.0),
        )

    @property
    def has_valid_live_power(self) -> bool:
        """Match the production model's invalid all-zero sample guard."""
        return not (
            self.load_w <= 1.0
            and self.grid_import_w <= 1.0
            and self.grid_export_w <= 1.0
            and self.pv_w <= 1.0
        )


@dataclass(frozen=True, slots=True)
class LegacyForecastTarget:
    """One requested slot and its already normalized weather factor."""

    local_start: dt.datetime
    weather_factor: float


@dataclass(frozen=True, slots=True)
class LegacyForecastConfig:
    """Explicit legacy model state required for deterministic shadowing."""

    timezone: str
    load_bias: float
    load_slot_biases: tuple[float, ...]
    pv_global_bias: float
    pv_slot_biases: tuple[float, ...]
    current_weather_factor: float
    current_pv_w: float | None
    tomorrow_date: str
    tomorrow_energy_kwh: float | None
    capacity_events: tuple[tuple[str, float, float], ...]

    def __post_init__(self) -> None:
        if len(self.load_slot_biases) != 96 or len(self.pv_slot_biases) != 96:
            raise ValueError("legacy slot bias arrays must contain 96 values")
        if not self.capacity_events:
            raise ValueError("at least one PV capacity event is required")


def build_legacy_forecast(
    request: ForecastRequest,
    samples: tuple[LegacyForecastSample, ...],
    targets: tuple[LegacyForecastTarget, ...],
    load_context: LoadForecastContext,
    config: LegacyForecastConfig,
) -> ForecastBundle:
    """Recalculate the current load/PV point forecast without side effects."""
    if len(request.slots) != len(targets):
        raise ValueError("forecast targets must match the requested slot grid")

    timezone = ZoneInfo(config.timezone)
    heat_pump_w = next(
        (
            driver.power_w
            for driver in load_context.drivers
            if driver.driver_key == "heat_pump"
        ),
        0.0,
    )
    now_local = dt.datetime.fromtimestamp(
        request.as_of_ms / 1000.0, tz=dt.timezone.utc
    ).astimezone(timezone)

    preliminary: list[tuple[float, float, LegacyForecastTarget]] = []
    for target in targets:
        slot = _slot_index(target.local_start)
        load_w = _forecast_load_w(
            samples,
            target.local_start,
            timezone,
            heat_pump_w,
            now_local,
            config.load_bias,
            config.load_slot_biases[slot],
        )
        pv_w = _forecast_pv_w(
            samples,
            target.local_start,
            timezone,
            target.weather_factor * config.pv_global_bias,
            config.pv_slot_biases[slot],
            config.capacity_events,
        )
        preliminary.append((load_w, pv_w, target))

    if config.current_pv_w is not None and config.current_pv_w > 50.0:
        current_weather_ref = max(0.2, config.current_weather_factor)
        anchored = []
        for load_w, pv_w, target in preliminary:
            horizon_h = max(
                0.0, (target.local_start - now_local).total_seconds() / 3600.0
            )
            if 0.0 <= horizon_h <= PV_NOWCAST_BLEND_HOURS and pv_w > 1.0:
                blend = 1.0 - (horizon_h / PV_NOWCAST_BLEND_HOURS)
                persistence_pv = max(
                    0.0,
                    config.current_pv_w
                    * (max(0.2, target.weather_factor) / current_weather_ref),
                )
                pv_w = max(pv_w, pv_w * (1.0 - blend) + persistence_pv * blend)
            anchored.append((load_w, pv_w, target))
        preliminary = anchored

    tomorrow_scale = _tomorrow_scale(preliminary, config)
    load_slots = []
    pv_slots = []
    for slot_key, (load_w, pv_w, target) in zip(
        request.slots, preliminary, strict=True
    ):
        if target.local_start.date().isoformat() == config.tomorrow_date:
            pv_w *= tomorrow_scale
        load_slots.append(
            ForecastSlot(slot_key, QuantileEnergy(max(0.0, load_w) * SLOT_H / 1000.0))
        )
        pv_slots.append(
            ForecastSlot(slot_key, QuantileEnergy(max(0.0, pv_w) * SLOT_H / 1000.0))
        )

    training_cutoff_ms = min(
        request.as_of_ms,
        int(max((sample.ts_s for sample in samples), default=0.0) * 1000),
    )
    forecast_id = f"legacy-{request.as_of_ms}"
    return ForecastBundle(
        load=LoadForecast(
            forecast_id=f"{forecast_id}-load",
            generated_at_ms=request.as_of_ms,
            training_cutoff_ms=training_cutoff_ms,
            model_version="legacy-load-v1",
            slots=tuple(load_slots),
        ),
        pv=PvForecast(
            forecast_id=f"{forecast_id}-pv",
            generated_at_ms=request.as_of_ms,
            training_cutoff_ms=training_cutoff_ms,
            model_version="legacy-pv-v1",
            slots=tuple(pv_slots),
        ),
    )


def _forecast_load_w(
    samples: tuple[LegacyForecastSample, ...],
    target: dt.datetime,
    timezone: ZoneInfo,
    heat_pump_w: float,
    now_local: dt.datetime,
    load_bias: float,
    slot_bias: float,
) -> float:
    target_slot = _slot_index(target)
    target_weekday = target.weekday()
    target_is_weekend = target_weekday >= 5
    same_slot: list[float] = []
    same_slot_weekday: list[float] = []
    same_slot_weektype: list[float] = []
    recent: list[float] = []
    recent_cutoff = samples[-1].ts_s - 7200 if samples else 0.0

    for sample in samples[-6000:]:
        sample_local = dt.datetime.fromtimestamp(
            sample.ts_s, tz=dt.timezone.utc
        ).astimezone(timezone)
        if not sample.has_valid_live_power:
            continue
        if _slot_index(sample_local) == target_slot:
            same_slot.append(sample.load_w)
            if sample_local.weekday() == target_weekday:
                same_slot_weekday.append(sample.load_w)
            if (sample_local.weekday() >= 5) == target_is_weekend:
                same_slot_weektype.append(sample.load_w)
        if sample.ts_s >= recent_cutoff:
            recent.append(sample.load_w)

    base_all = _median(same_slot[-60:], 450.0)
    base_weekday = _median(same_slot_weekday[-20:], base_all)
    base_weektype = _median(same_slot_weektype[-30:], base_all)
    trend = sum(recent) / len(recent) if recent else base_all
    load_w = 0.45 * base_weekday + 0.25 * base_weektype + 0.15 * base_all + 0.15 * trend

    horizon_h = max(0.0, (target - now_local).total_seconds() / 3600.0)
    if horizon_h <= 6.0:
        heat_pump_excess = max(0.0, heat_pump_w - 500.0)
        load_w += 0.22 * heat_pump_excess * math.exp(-horizon_h / 1.5)

    return max(
        0.0,
        load_w * _clamp(load_bias, 0.6, 1.6) * _clamp(slot_bias, 0.7, 1.4),
    )


def _forecast_pv_w(
    samples: tuple[LegacyForecastSample, ...],
    target: dt.datetime,
    timezone: ZoneInfo,
    weather_factor: float,
    slot_bias: float,
    capacity_events: tuple[tuple[str, float, float], ...],
) -> float:
    target_slot = _slot_index(target)
    target_is_weekend = target.weekday() >= 5
    target_kwp, target_inverter_kw = _capacity_at(target, timezone, capacity_events)
    same_slot: list[float] = []
    same_slot_weektype: list[float] = []

    for sample in samples[-6000:]:
        sample_local = dt.datetime.fromtimestamp(
            sample.ts_s, tz=dt.timezone.utc
        ).astimezone(timezone)
        if not sample.has_valid_live_power and sample.pv_w <= 1.0:
            continue
        if _slot_index(sample_local) != target_slot:
            continue
        sample_kwp, _ = _capacity_at(sample_local, timezone, capacity_events)
        normalized_pv = sample.pv_w * (target_kwp / max(0.1, sample_kwp))
        same_slot.append(normalized_pv)
        if (sample_local.weekday() >= 5) == target_is_weekend:
            same_slot_weektype.append(normalized_pv)

    base_all = _median(same_slot[-60:], 0.0)
    base_weektype = _median(same_slot_weektype[-30:], base_all)
    pv_w = 0.65 * base_weektype + 0.35 * base_all
    if target.hour < 6 or target.hour > 20:
        pv_w = 0.0
    forecast_w = max(0.0, pv_w * weather_factor * _clamp(slot_bias, 0.6, 1.5))
    return min(forecast_w, max(0.0, target_inverter_kw * 1000.0))


def _tomorrow_scale(
    preliminary: list[tuple[float, float, LegacyForecastTarget]],
    config: LegacyForecastConfig,
) -> float:
    if config.tomorrow_energy_kwh is None or config.tomorrow_energy_kwh <= 0.0:
        return 1.0
    forecast_kwh = sum(
        max(0.0, pv_w) * SLOT_H / 1000.0
        for _load_w, pv_w, target in preliminary
        if target.local_start.date().isoformat() == config.tomorrow_date
    )
    if forecast_kwh <= 0.05:
        return 1.0
    return _clamp(config.tomorrow_energy_kwh / forecast_kwh, 0.25, 4.0)


def _capacity_at(
    value: dt.datetime,
    timezone: ZoneInfo,
    events: tuple[tuple[str, float, float], ...],
) -> tuple[float, float]:
    target = value.astimezone(timezone)
    generator_kwp = float(events[0][1])
    inverter_kw = float(events[0][2])
    for event_iso, event_kwp, event_inverter_kw in events:
        event = dt.datetime.fromisoformat(event_iso).astimezone(timezone)
        if target < event:
            break
        generator_kwp = float(event_kwp)
        inverter_kw = float(event_inverter_kw)
    return generator_kwp, inverter_kw


def _slot_index(value: dt.datetime) -> int:
    return value.hour * 4 + value.minute // 15


def _median(values: list[float], fallback: float) -> float:
    return float(statistics.median(values)) if values else fallback


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))

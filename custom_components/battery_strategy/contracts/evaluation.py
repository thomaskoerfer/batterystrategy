"""Contracts for forecast comparison and later actual maturation."""

from __future__ import annotations

from dataclasses import dataclass

from .common import DataQuality, SlotKey, require_nonnegative


@dataclass(frozen=True, slots=True)
class ForecastEvaluationPoint:
    """Production and shadow values for one target slot and lead time."""

    generated_at_ms: int
    target: SlotKey
    lead_minutes: int
    production_load_kwh: float
    shadow_load_kwh: float
    production_pv_kwh: float
    shadow_pv_kwh: float
    actual_load_kwh: float | None = None
    actual_pv_kwh: float | None = None
    actual_quality: DataQuality | None = None

    def __post_init__(self) -> None:
        if self.generated_at_ms < 0 or self.lead_minutes <= 0:
            raise ValueError("evaluation generation and lead time must be positive")
        for name in (
            "production_load_kwh",
            "shadow_load_kwh",
            "production_pv_kwh",
            "shadow_pv_kwh",
        ):
            require_nonnegative(name, getattr(self, name))
        for name in ("actual_load_kwh", "actual_pv_kwh"):
            value = getattr(self, name)
            if value is not None:
                require_nonnegative(name, value)


@dataclass(frozen=True, slots=True)
class ForecastEvaluationRun:
    """One isolated comparison emitted by the shadow forecast runner."""

    generated_at_ms: int
    status: str
    reason: str | None
    history_slot_count: int
    load_usable_slots: int
    pv_usable_slots: int
    history_span_days: float
    load_parity_mae_w: float | None
    load_parity_bias_w: float | None
    pv_parity_mae_w: float | None
    pv_parity_bias_w: float | None
    points: tuple[ForecastEvaluationPoint, ...] = ()
    authoritative: bool = False

    def __post_init__(self) -> None:
        if self.generated_at_ms < 0 or not self.status:
            raise ValueError("evaluation run identity is required")
        if min(
            self.history_slot_count, self.load_usable_slots, self.pv_usable_slots
        ) < 0:
            raise ValueError("evaluation slot counts must be non-negative")
        require_nonnegative("history_span_days", self.history_span_days)
        if self.authoritative:
            raise ValueError("forecast evaluation must remain non-authoritative")

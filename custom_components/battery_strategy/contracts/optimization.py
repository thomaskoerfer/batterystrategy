"""Contracts between forecasting, market data and pure optimization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .common import (
    SlotKey,
    require_finite,
    require_nonnegative,
    require_percentage,
    require_slots_sorted_unique,
)
from .forecasting import ForecastBundle


class PlanMode(StrEnum):
    """Economic action selected for a planning slot."""

    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"


@dataclass(frozen=True, slots=True)
class MarketSlot:
    """Import and export valuation for one slot."""

    slot: SlotKey
    import_price_ct_per_kwh: float
    export_price_ct_per_kwh: float = 0.0
    source: str = "configured_price_entity"

    def __post_init__(self) -> None:
        require_finite("import_price_ct_per_kwh", self.import_price_ct_per_kwh)
        require_finite("export_price_ct_per_kwh", self.export_price_ct_per_kwh)
        if not self.source:
            raise ValueError("market source is required")


@dataclass(frozen=True, slots=True)
class BatteryState:
    """Battery state at the optimization boundary."""

    captured_at_ms: int
    soc_pct: float

    def __post_init__(self) -> None:
        if self.captured_at_ms < 0:
            raise ValueError("captured_at_ms must be non-negative")
        require_percentage("soc_pct", self.soc_pct)


@dataclass(frozen=True, slots=True)
class BatteryConstraints:
    """Physical battery limits, independent of market policy."""

    capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    max_charge_power_w: float
    max_discharge_power_w: float
    round_trip_efficiency: float

    def __post_init__(self) -> None:
        for name in (
            "max_charge_power_w",
            "max_discharge_power_w",
            "round_trip_efficiency",
        ):
            require_nonnegative(name, getattr(self, name))
        if self.capacity_kwh <= 0.0:
            raise ValueError("capacity_kwh must be positive")
        require_percentage("min_soc_pct", self.min_soc_pct)
        require_percentage("max_soc_pct", self.max_soc_pct)
        if self.min_soc_pct >= self.max_soc_pct:
            raise ValueError("min_soc_pct must be below max_soc_pct")
        if not 0.0 < self.round_trip_efficiency <= 1.0:
            raise ValueError("round_trip_efficiency must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class CommercialPolicy:
    """Commercial assumptions used by optimization only."""

    min_margin_ct_per_kwh: float
    terminal_value_ct_per_kwh: float = 0.0
    export_opportunity_ct_per_kwh: float = 0.0
    discharge_floor_ct_per_kwh: float | None = None
    pv_charging_allowed: bool = True
    grid_charging_allowed: bool = True
    discharge_allowed: bool = True
    pv_recovery_confidence: float = 0.75
    pv_recovery_reserve_kwh: float = 0.30

    def __post_init__(self) -> None:
        require_nonnegative("min_margin_ct_per_kwh", self.min_margin_ct_per_kwh)
        require_nonnegative("terminal_value_ct_per_kwh", self.terminal_value_ct_per_kwh)
        require_nonnegative(
            "export_opportunity_ct_per_kwh", self.export_opportunity_ct_per_kwh
        )
        if self.discharge_floor_ct_per_kwh is not None:
            require_nonnegative(
                "discharge_floor_ct_per_kwh", self.discharge_floor_ct_per_kwh
            )
        if not 0.0 <= self.pv_recovery_confidence <= 1.0:
            raise ValueError("pv_recovery_confidence must be in [0, 1]")
        require_nonnegative("pv_recovery_reserve_kwh", self.pv_recovery_reserve_kwh)


@dataclass(frozen=True, slots=True)
class OptimizationProblem:
    """Complete deterministic input to a pure optimizer."""

    problem_id: str
    as_of_ms: int
    forecast: ForecastBundle
    market: tuple[MarketSlot, ...]
    battery: BatteryState
    constraints: BatteryConstraints
    policy: CommercialPolicy

    def __post_init__(self) -> None:
        if not self.problem_id or self.as_of_ms < 0:
            raise ValueError("problem_id and non-negative as_of_ms are required")
        if (
            self.forecast.load.generated_at_ms > self.as_of_ms
            or self.forecast.pv.generated_at_ms > self.as_of_ms
        ):
            raise ValueError("forecasts cannot be newer than optimization as_of_ms")
        forecast_slots = tuple(item.slot for item in self.forecast.load.slots)
        market_slots = tuple(item.slot for item in self.market)
        if forecast_slots != market_slots:
            raise ValueError("market and forecast must use the same slot grid")
        if self.battery.captured_at_ms > self.as_of_ms:
            raise ValueError("battery state cannot be newer than optimization as_of_ms")


@dataclass(frozen=True, slots=True)
class BatteryPlanSlot:
    """Economic intent for one slot, expressed as energy and budgets."""

    slot: SlotKey
    mode: PlanMode
    pv_charge_allowed: bool
    grid_charge_allowed: bool
    planned_charge_kwh: float
    planned_discharge_kwh: float
    required_charge_kwh: float
    discharge_budget_kwh: float
    expected_soc_start_pct: float
    expected_soc_end_pct: float
    planned_pv_charge_kwh: float = 0.0
    planned_grid_charge_kwh: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "planned_charge_kwh",
            "planned_discharge_kwh",
            "required_charge_kwh",
            "discharge_budget_kwh",
            "planned_pv_charge_kwh",
            "planned_grid_charge_kwh",
        ):
            require_nonnegative(name, getattr(self, name))
        require_percentage("expected_soc_start_pct", self.expected_soc_start_pct)
        require_percentage("expected_soc_end_pct", self.expected_soc_end_pct)
        if self.planned_charge_kwh > 0.0 and self.planned_discharge_kwh > 0.0:
            raise ValueError("a slot cannot plan charge and discharge simultaneously")
        if self.required_charge_kwh > self.planned_charge_kwh:
            raise ValueError("required charge cannot exceed planned charge")
        if abs(
            self.planned_charge_kwh
            - self.planned_pv_charge_kwh
            - self.planned_grid_charge_kwh
        ) > 1e-9:
            raise ValueError("planned charge must equal its PV and grid sources")
        if self.planned_grid_charge_kwh > 0.0 and not self.grid_charge_allowed:
            raise ValueError("planned grid charge requires grid permission")
        if self.required_charge_kwh > 0.0 and self.planned_grid_charge_kwh <= 0.0:
            raise ValueError("required charge requires a grid commitment")
        if self.mode == PlanMode.IDLE and (
            self.planned_charge_kwh > 0.0 or self.planned_discharge_kwh > 0.0
        ):
            raise ValueError("idle plan slots cannot contain a planned flow")
        if self.mode == PlanMode.CHARGE and self.planned_discharge_kwh > 0.0:
            raise ValueError("charge plan slots cannot plan discharge")
        if self.mode == PlanMode.DISCHARGE and self.planned_charge_kwh > 0.0:
            raise ValueError("discharge plan slots cannot plan charge")
        if self.planned_discharge_kwh > self.discharge_budget_kwh + 1e-9:
            raise ValueError("planned discharge cannot exceed its commercial budget")


@dataclass(frozen=True, slots=True)
class BatteryPlan:
    """Versioned optimizer result consumed by the plan compiler."""

    plan_id: str
    problem_id: str
    generated_at_ms: int
    optimizer_version: str
    constraints: BatteryConstraints
    slots: tuple[BatteryPlanSlot, ...]
    baseline_cost_eur: float
    optimized_cost_eur: float

    def __post_init__(self) -> None:
        if not self.plan_id or not self.problem_id or not self.optimizer_version:
            raise ValueError("plan identity and optimizer_version are required")
        if self.generated_at_ms < 0:
            raise ValueError("generated_at_ms must be non-negative")
        require_finite("baseline_cost_eur", self.baseline_cost_eur)
        require_finite("optimized_cost_eur", self.optimized_cost_eur)
        slot_keys = tuple(item.slot for item in self.slots)
        require_slots_sorted_unique(slot_keys)


class Optimizer(Protocol):
    """Side-effect-free economic optimizer boundary."""

    def optimize(self, problem: OptimizationProblem) -> BatteryPlan: ...

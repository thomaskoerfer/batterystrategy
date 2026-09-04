"""Contracts between plan compilation, live control and actuation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .common import (
    DataQuality,
    SlotKey,
    require_finite,
    require_nonnegative,
    require_percentage,
)
from .optimization import BatteryPlan


class CommandMode(StrEnum):
    """Unambiguous actuator direction."""

    IDLE = "idle"
    INPUT = "input"
    OUTPUT = "output"


class AutomaticDischargeMode(StrEnum):
    """Operator-selected automatic discharge behavior."""

    OFF = "off"
    LOAD_FOLLOWING = "load_following"
    PRICE_SENSITIVE = "price_sensitive"


class ManualControlMode(StrEnum):
    """Explicit operator override evaluated ahead of automatic control."""

    OFF = "off"
    CHARGE = "charge"
    DISCHARGE = "discharge"


@dataclass(frozen=True, slots=True)
class SlotProgress:
    """Measured progress used when compiling a current-slot directive."""

    slot: SlotKey
    charged_kwh: float
    discharged_kwh: float
    soc_pct: float

    def __post_init__(self) -> None:
        require_nonnegative("charged_kwh", self.charged_kwh)
        require_nonnegative("discharged_kwh", self.discharged_kwh)
        require_percentage("soc_pct", self.soc_pct)


@dataclass(frozen=True, slots=True)
class PlanCompilationState:
    """Explicit economic commitment latched for one active slot."""

    slot: SlotKey | None = None
    committed_plan_id: str | None = None
    required_charge_commitment_kwh: float = 0.0
    discharge_budget_commitment_kwh: float = 0.0
    grid_charge_allowed: bool = False

    def __post_init__(self) -> None:
        require_nonnegative(
            "required_charge_commitment_kwh",
            self.required_charge_commitment_kwh,
        )
        require_nonnegative(
            "discharge_budget_commitment_kwh",
            self.discharge_budget_commitment_kwh,
        )
        if self.slot is None:
            if (
                self.committed_plan_id is not None
                or self.required_charge_commitment_kwh > 0.0
                or self.discharge_budget_commitment_kwh > 0.0
                or self.grid_charge_allowed
            ):
                raise ValueError("empty compilation state cannot contain a commitment")
        elif not self.committed_plan_id:
            raise ValueError("active compilation state requires committed_plan_id")
        if self.required_charge_commitment_kwh > 0.0 and not self.grid_charge_allowed:
            raise ValueError("required charge commitment requires grid permission")


@dataclass(frozen=True, slots=True)
class PlanLiveDirective:
    """Complete commercial permission consumed by fast live control."""

    directive_id: str
    plan_id: str
    issued_at_ms: int
    slot: SlotKey
    pv_charge_allowed: bool
    grid_charge_allowed: bool
    required_charge_power_w: float
    required_charge_remaining_kwh: float
    max_pv_charge_power_w: float
    max_grid_charge_power_w: float
    max_discharge_power_w: float
    discharge_budget_remaining_kwh: float
    min_soc_pct: float
    max_soc_pct: float

    def __post_init__(self) -> None:
        if not self.directive_id or not self.plan_id or self.issued_at_ms < 0:
            raise ValueError("directive identity and issued_at_ms are required")
        for name in (
            "required_charge_remaining_kwh",
            "required_charge_power_w",
            "max_pv_charge_power_w",
            "max_grid_charge_power_w",
            "max_discharge_power_w",
            "discharge_budget_remaining_kwh",
        ):
            require_nonnegative(name, getattr(self, name))
        require_percentage("min_soc_pct", self.min_soc_pct)
        require_percentage("max_soc_pct", self.max_soc_pct)
        if self.min_soc_pct >= self.max_soc_pct:
            raise ValueError("directive min_soc_pct must be below max_soc_pct")
        if not self.grid_charge_allowed and self.required_charge_power_w > 0.0:
            raise ValueError("required charge power requires grid_charge_allowed")
        if not self.grid_charge_allowed and self.max_grid_charge_power_w > 0.0:
            raise ValueError("grid charge power requires grid_charge_allowed")
        if self.required_charge_power_w > self.max_grid_charge_power_w:
            raise ValueError("required charge power exceeds grid charge limit")
        if not self.pv_charge_allowed and self.max_pv_charge_power_w > 0.0:
            raise ValueError("PV charge power requires pv_charge_allowed")


@dataclass(frozen=True, slots=True)
class LiveMeasurements:
    """One normalized live snapshot; all named power flows are positive."""

    captured_at_ms: int
    grid_import_w: float
    grid_export_w: float
    pv_generation_w: float
    battery_charge_w: float
    battery_discharge_w: float
    ev_charge_w: float
    soc_pct: float
    quality: DataQuality = field(default_factory=DataQuality)

    def __post_init__(self) -> None:
        if self.captured_at_ms < 0:
            raise ValueError("captured_at_ms must be non-negative")
        for name in (
            "grid_import_w",
            "grid_export_w",
            "pv_generation_w",
            "battery_charge_w",
            "battery_discharge_w",
            "ev_charge_w",
        ):
            require_nonnegative(name, getattr(self, name))
        require_percentage("soc_pct", self.soc_pct)
        if self.battery_charge_w > 0.0 and self.battery_discharge_w > 0.0:
            raise ValueError("battery cannot charge and discharge simultaneously")


@dataclass(frozen=True, slots=True)
class LivePolicy:
    """User policy evaluated only against live EV and meter measurements."""

    pv_to_ev_first: bool
    discharge_during_ev_charging: bool
    battery_may_feed_ev: bool
    ev_active_threshold_w: float
    min_command_power_w: float
    max_charge_power_w: float
    max_discharge_power_w: float
    automatic_discharge_mode: AutomaticDischargeMode = (
        AutomaticDischargeMode.LOAD_FOLLOWING
    )
    manual_mode: ManualControlMode = ManualControlMode.OFF
    manual_power_w: float = 0.0

    def __post_init__(self) -> None:
        require_nonnegative("ev_active_threshold_w", self.ev_active_threshold_w)
        require_nonnegative("min_command_power_w", self.min_command_power_w)
        require_nonnegative("max_charge_power_w", self.max_charge_power_w)
        require_nonnegative("max_discharge_power_w", self.max_discharge_power_w)
        require_nonnegative("manual_power_w", self.manual_power_w)


@dataclass(frozen=True, slots=True)
class LiveControlState:
    """Explicit previous-command state used by a pure live controller."""

    previous_mode: CommandMode
    previous_power_w: float
    previous_command_at_ms: int | None
    direction: CommandMode | None = None
    charge_block_until_ms: int | None = None
    last_charge_at_ms: int | None = None

    def __post_init__(self) -> None:
        require_nonnegative("previous_power_w", self.previous_power_w)
        if self.previous_command_at_ms is not None and self.previous_command_at_ms < 0:
            raise ValueError("previous_command_at_ms must be non-negative")
        if self.previous_mode == CommandMode.IDLE and self.previous_power_w != 0.0:
            raise ValueError("idle live-control state must have zero power")
        for name in (
            "charge_block_until_ms",
            "last_charge_at_ms",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.direction == CommandMode.IDLE:
            raise ValueError("live-control direction is input, output or unknown")


@dataclass(frozen=True, slots=True)
class LiveDiagnostics:
    """Derived live values kept separate from executable commands."""

    residual_with_ev_w: float
    residual_no_ev_w: float
    pv_surplus_w: float
    allowed_discharge_load_w: float
    house_load_total_w: float
    house_load_no_ev_w: float

    def __post_init__(self) -> None:
        for name in ("residual_with_ev_w", "residual_no_ev_w"):
            require_finite(name, getattr(self, name))
        for name in (
            "pv_surplus_w",
            "allowed_discharge_load_w",
            "house_load_total_w",
            "house_load_no_ev_w",
        ):
            require_nonnegative(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class BatteryCommand:
    """Validated command passed to the single actuator path."""

    command_id: str
    directive_id: str
    created_at_ms: int
    valid_until_ms: int
    mode: CommandMode
    power_w: float
    reason: str

    def __post_init__(self) -> None:
        if not self.command_id or not self.directive_id or not self.reason:
            raise ValueError("command identity, directive and reason are required")
        if self.created_at_ms < 0 or self.valid_until_ms <= self.created_at_ms:
            raise ValueError("command validity interval is invalid")
        require_nonnegative("power_w", self.power_w)
        if self.mode == CommandMode.IDLE and self.power_w != 0.0:
            raise ValueError("idle commands must have zero power")
        if self.mode != CommandMode.IDLE and self.power_w <= 0.0:
            raise ValueError("active commands must have positive power")


@dataclass(frozen=True, slots=True)
class LiveControlResult:
    """Complete output of one pure live-control evaluation."""

    command: BatteryCommand
    state: LiveControlState
    diagnostics: LiveDiagnostics


@dataclass(frozen=True, slots=True)
class ActuationResult:
    """Observable result of one actuator request."""

    command_id: str
    applied: bool
    applied_at_ms: int
    detail: str

    def __post_init__(self) -> None:
        if not self.command_id or self.applied_at_ms < 0 or not self.detail:
            raise ValueError(
                "actuation result identity, timestamp and detail are required"
            )


class PlanCompiler(Protocol):
    """Pure conversion from plan intent and measured progress to a directive."""

    def compile(
        self,
        plan: BatteryPlan,
        progress: SlotProgress,
        state: PlanCompilationState,
        issued_at_ms: int,
    ) -> tuple[PlanLiveDirective, PlanCompilationState]: ...


class LiveController(Protocol):
    """Fast, side-effect-free meter-following command calculation."""

    def command(
        self,
        directive: PlanLiveDirective,
        measurements: LiveMeasurements,
        policy: LivePolicy,
        state: LiveControlState,
    ) -> LiveControlResult: ...


class BatteryActuator(Protocol):
    """The only port allowed to write battery hardware controls."""

    async def apply(self, command: BatteryCommand) -> ActuationResult: ...

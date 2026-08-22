"""Typed planning models for the Battery Strategy optimizer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlanPoint:
    """Optimized battery action for a planning slot."""

    ts_ms: int
    date: str
    price_ct: float
    load_fc_w: int
    pv_fc_w: int
    grid_import_fc_w: int
    grid_export_fc_w: int
    grid_net_fc_w: int
    mode: str
    power_w: int
    charge_fc_w: int
    discharge_fc_w: int
    soc_pct: float
    discharge_budget_kwh: float = 0.0
    pv_charge_fc_w: int | None = None
    grid_charge_fc_w: int | None = None
    required_charge_fc_w: int | None = None


@dataclass(frozen=True)
class DailyCost:
    """Baseline and optimized grid costs for one day."""

    base_eur: float
    with_bat_eur: float

    @property
    def saving_eur(self) -> float:
        """Return savings compared with no battery strategy."""
        return round(self.base_eur - self.with_bat_eur, 3)


@dataclass(frozen=True)
class PlanLiveDirective:
    """Minimal plan output consumed by live meter-following control."""

    slot_id: str
    slot_start_ts: int
    slot_end_ts: int
    pv_charge_allowed: bool
    must_charge_w: int
    must_charge_remaining_kwh: float
    grid_charge_allowed: bool
    discharge_budget_kwh: float
    battery_min_soc_pct: float
    battery_max_soc_pct: float


@dataclass(frozen=True)
class StrategyPlan:
    """Full optimizer output."""

    points: list[PlanPoint]
    current_mode: str
    current_power_w: int
    reason: str
    daily_costs: dict[str, DailyCost] = field(default_factory=dict)
    price_stats: dict[str, float | None] = field(default_factory=dict)
    load_forecast_next_1h_kwh: float = 0.0
    pv_forecast_corrected_next_1h_kwh: float = 0.0
    net_load_forecast_next_1h_kwh: float = 0.0
    grid_import_forecast_next_1h_kwh: float = 0.0
    grid_export_forecast_next_1h_kwh: float = 0.0
    virtual_soc_end_tomorrow_pct: float = 0.0
    override_active: bool = False

    def profile(self, key: str, date: str | None = None) -> list[list[float]]:
        """Return a chart profile for a plan point key."""
        selected = self.points if date is None else [p for p in self.points if p.date == date]
        return [[p.ts_ms, getattr(p, key)] for p in selected]

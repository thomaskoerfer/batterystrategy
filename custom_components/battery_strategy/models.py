"""Pure data models for Battery Strategy calculations."""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    DISCHARGE_LOAD,
    GRID_CHARGING_OFF,
    MANUAL_OFF,
    PV_CHARGING_ON,
)


@dataclass(frozen=True)
class StrategyOptions:
    """User-visible strategy settings."""

    pv_charging: str = PV_CHARGING_ON
    grid_charging: str = GRID_CHARGING_OFF
    discharge: str = DISCHARGE_LOAD
    pv_to_ev_first: bool = True
    discharge_during_ev_charging: bool = True
    battery_may_feed_ev: bool = False
    ev_active_threshold_w: float = 300.0
    min_soc_pct: float = 10.0
    max_soc_pct: float = 100.0
    max_charge_power_w: float = 2400.0
    max_discharge_power_w: float = 2400.0
    min_command_power_w: float = 20.0
    min_command_delta_w: float = 20.0
    round_trip_efficiency: float = 0.80
    min_margin_ct_per_kwh: float = 2.0
    planning_horizon_h: int = 48
    feed_in_tariff_ct_per_kwh: float = 0.0
    battery_capacity_kwh: float = 6.0
    pv_capacity_kwp: float = 0.0
    pv_inverter_power_kw: float = 0.0
    manual_mode: str = MANUAL_OFF
    manual_power_w: float = 0.0

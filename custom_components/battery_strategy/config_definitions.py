"""Canonical option defaults and numeric constraints for Battery Strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_PV_CAPACITY_KWP,
    CONF_PV_INVERTER_POWER_KW,
)
from .models import StrategyOptions


@dataclass(frozen=True, slots=True)
class NumericOptionDefinition:
    """One numeric option and its deliberately narrower form constraints."""

    key: str
    default: float
    minimum: float
    maximum: float
    step: float
    unit: str | None = None
    exposed_as_entity: bool = True
    form_default: float | None = None
    form_minimum: float | None = None
    form_maximum: float | None = None
    form_step: float | None = None

    @property
    def config_minimum(self) -> float:
        return self.minimum if self.form_minimum is None else self.form_minimum

    @property
    def config_default(self) -> float:
        return self.default if self.form_default is None else self.form_default

    @property
    def config_maximum(self) -> float:
        return self.maximum if self.form_maximum is None else self.form_maximum

    @property
    def config_step(self) -> float:
        return self.step if self.form_step is None else self.form_step


OPTION_DEFAULTS: dict[str, object] = {
    **asdict(StrategyOptions()),
    "strategy_enabled": False,
    "trace_enabled": False,
}

NUMERIC_OPTIONS = {
    item.key: item
    for item in (
        NumericOptionDefinition("manual_power_w", 0.0, 0.0, 2400.0, 50.0, "W"),
        NumericOptionDefinition(
            "ev_active_threshold_w",
            300.0,
            0.0,
            11000.0,
            50.0,
            "W",
            form_maximum=5000.0,
        ),
        NumericOptionDefinition("min_soc_pct", 10.0, 0.0, 100.0, 1.0, "%"),
        NumericOptionDefinition("max_soc_pct", 100.0, 0.0, 100.0, 1.0, "%"),
        NumericOptionDefinition("max_charge_power_w", 2400.0, 0.0, 2400.0, 50.0, "W"),
        NumericOptionDefinition(
            "max_discharge_power_w", 2400.0, 0.0, 2400.0, 50.0, "W"
        ),
        NumericOptionDefinition("min_command_power_w", 20.0, 0.0, 500.0, 10.0, "W"),
        # Keep the established form suggestion while making the actuator's
        # effective 20 W fallback the canonical persisted/runtime default.
        NumericOptionDefinition(
            "min_command_delta_w",
            20.0,
            0.0,
            500.0,
            10.0,
            "W",
            form_default=5.0,
            form_step=5.0,
        ),
        NumericOptionDefinition(
            "round_trip_efficiency", 0.80, 0.1, 1.0, 0.01, form_minimum=0.5
        ),
        NumericOptionDefinition(
            "min_margin_ct_per_kwh",
            2.0,
            0.0,
            50.0,
            0.1,
            "ct/kWh",
            form_maximum=30.0,
        ),
        NumericOptionDefinition(
            "feed_in_tariff_ct_per_kwh", 0.0, 0.0, 50.0, 0.1, "ct/kWh"
        ),
        NumericOptionDefinition("planning_horizon_h", 48.0, 1.0, 48.0, 1.0, "h"),
        NumericOptionDefinition(
            CONF_BATTERY_CAPACITY_KWH,
            6.0,
            0.5,
            100.0,
            0.1,
            "kWh",
            exposed_as_entity=False,
        ),
        NumericOptionDefinition(
            CONF_PV_CAPACITY_KWP,
            0.0,
            0.0,
            100.0,
            0.1,
            "kWp",
            exposed_as_entity=False,
        ),
        NumericOptionDefinition(
            CONF_PV_INVERTER_POWER_KW,
            0.0,
            0.0,
            100.0,
            0.1,
            "kW",
            exposed_as_entity=False,
        ),
    )
}


def option_default(key: str):
    """Return the canonical default for a persisted option."""
    return OPTION_DEFAULTS[key]


def numeric_option(key: str) -> NumericOptionDefinition:
    """Return the canonical numeric definition for one option."""
    return NUMERIC_OPTIONS[key]

"""Sensors for Battery Strategy."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import BatteryStrategyEntity

STATE_CLASS_MEASUREMENT = SensorStateClass.MEASUREMENT


@dataclass(frozen=True, kw_only=True)
class BatteryStrategySensorDescription(SensorEntityDescription):
    """Description for a Battery Strategy sensor."""

    value_fn: Callable[[object], object]
    attr_fn: Callable[[object], dict] | None = None


def _command(data):
    return data["command"]


def _command_source(data):
    if not data.get("strategy_enabled", False):
        return "external_control_strategy_disabled"
    if not data.get("send_commands", True):
        return "strategy_not_sending"
    return "battery_strategy"


def _inputs(data):
    return data["inputs"]


def _plan(data):
    return data["plan"]


def _plan_to_live(data):
    return data["plan_to_live"]


def _optimizer_discharge_budget_kwh(data) -> float:
    points = getattr(_plan(data), "points", [])
    if not points:
        return 0.0
    return round(max(0.0, float(getattr(points[0], "discharge_budget_kwh", 0.0))), 3)


def _raw_float(data, key: str, default: float = 0.0) -> float:
    try:
        return float((data.get("optimizer_attrs") or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _actual_charge_total_kwh(data) -> float:
    return round(
        _raw_float(data, "actual_battery_charge_grid_today_kwh")
        + _raw_float(data, "actual_battery_charge_pv_today_kwh"),
        3,
    )


def _actual_avg_charge_price_ct(data) -> float:
    kwh = _actual_charge_total_kwh(data)
    if kwh <= 0.01:
        return 0.0
    return round(
        (_raw_float(data, "actual_battery_charge_cost_today_eur") / kwh) * 100.0, 1
    )


def _actual_avg_discharge_price_ct(data) -> float:
    kwh = _raw_float(data, "actual_battery_discharge_credited_today_kwh")
    if kwh <= 0.01:
        return 0.0
    return round(
        (_raw_float(data, "actual_battery_discharge_credit_today_eur") / kwh) * 100.0, 1
    )


def _today(data):
    return dt_util.now().date().isoformat()


def _tomorrow(data):
    return (dt_util.now().date() + dt.timedelta(days=1)).isoformat()


SENSORS: tuple[BatteryStrategySensorDescription, ...] = (
    BatteryStrategySensorDescription(
        key="mode", name="Mode", value_fn=lambda data: _command(data).mode
    ),
    BatteryStrategySensorDescription(
        key="command_power",
        name="Command Power",
        value_fn=lambda data: _command(data).power_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="command_source", name="Command Source", value_fn=_command_source
    ),
    BatteryStrategySensorDescription(
        key="reason", name="Reason", value_fn=lambda data: _command(data).reason
    ),
    BatteryStrategySensorDescription(
        key="residual_with_ev",
        name="Residual With EV",
        value_fn=lambda data: _command(data).residual_with_ev_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="residual_no_ev",
        name="Residual No EV",
        value_fn=lambda data: _command(data).residual_no_ev_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="pv_surplus",
        name="PV Surplus",
        value_fn=lambda data: _command(data).pv_surplus_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="allowed_discharge_load",
        name="Allowed Discharge Load",
        value_fn=lambda data: _command(data).allowed_discharge_load_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="house_load_total",
        name="House Load Total",
        value_fn=lambda data: _command(data).house_load_total_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="house_load_no_ev",
        name="House Load No EV",
        value_fn=lambda data: _command(data).house_load_no_ev_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="grid_import",
        name="Grid Import",
        value_fn=lambda data: round(_inputs(data).grid_import_w),
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="grid_export",
        name="Grid Export",
        value_fn=lambda data: round(_inputs(data).grid_export_w),
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="battery_power",
        name="Battery Power",
        value_fn=lambda data: round(_inputs(data).battery_power_w),
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="ev_power",
        name="EV Power",
        value_fn=lambda data: round(_inputs(data).ev_power_w),
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="soc",
        name="SoC",
        value_fn=lambda data: round(_inputs(data).soc_pct, 1),
        attr_fn=lambda data: {
            "estimate_stale": bool(data.get("soc_estimate_stale", False)),
            "control_ready": bool(data.get("soc_control_ready", False)),
        },
        native_unit_of_measurement=PERCENTAGE,
    ),
    BatteryStrategySensorDescription(
        key="planned_mode",
        name="Planned Mode",
        value_fn=lambda data: _plan(data).current_mode,
    ),
    BatteryStrategySensorDescription(
        key="planned_power",
        name="Planned Power",
        value_fn=lambda data: _plan(data).current_power_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="planned_charge_power",
        name="Planned Charge Power",
        value_fn=lambda data: (
            _plan(data).current_power_w if _plan(data).current_mode == "input" else 0
        ),
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="planned_discharge_power",
        name="Planned Discharge Power",
        value_fn=lambda data: (
            _plan(data).current_power_w if _plan(data).current_mode == "output" else 0
        ),
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="plan_live_slot_start",
        name="Slot Start",
        value_fn=lambda data: _format_ts_ms(_plan_to_live(data).slot_start_ts),
    ),
    BatteryStrategySensorDescription(
        key="plan_live_slot_end",
        name="Slot End",
        value_fn=lambda data: _format_ts_ms(_plan_to_live(data).slot_end_ts),
    ),
    BatteryStrategySensorDescription(
        key="plan_live_pv_charge_allowed",
        name="PV Charge Allowed",
        value_fn=lambda data: "on" if _plan_to_live(data).pv_charge_allowed else "off",
    ),
    BatteryStrategySensorDescription(
        key="plan_live_must_charge",
        name="Must Charge",
        value_fn=lambda data: _plan_to_live(data).must_charge_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="plan_live_must_charge_remaining",
        name="Must Charge Remaining",
        value_fn=lambda data: _plan_to_live(data).must_charge_remaining_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="plan_live_grid_charge_allowed",
        name="Grid Charge Allowed",
        value_fn=lambda data: (
            "on" if _plan_to_live(data).grid_charge_allowed else "off"
        ),
    ),
    BatteryStrategySensorDescription(
        key="plan_live_discharge_budget",
        name="Live Remaining Discharge Budget",
        value_fn=lambda data: _plan_to_live(data).discharge_budget_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="optimizer_discharge_budget",
        name="Optimizer Discharge Budget",
        value_fn=_optimizer_discharge_budget_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="load_forecast_next_1h",
        name="Load Forecast Next 1h",
        value_fn=lambda data: _plan(data).load_forecast_next_1h_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="pv_forecast_corrected_next_1h",
        name="PV Forecast Corrected Next 1h",
        value_fn=lambda data: _plan(data).pv_forecast_corrected_next_1h_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="net_load_forecast_next_1h",
        name="Net Load Forecast Next 1h",
        value_fn=lambda data: _plan(data).net_load_forecast_next_1h_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="grid_import_forecast_next_1h",
        name="Grid Import Forecast Next 1h",
        value_fn=lambda data: _plan(data).grid_import_forecast_next_1h_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="grid_export_forecast_next_1h",
        name="Grid Export Forecast Next 1h",
        value_fn=lambda data: _plan(data).grid_export_forecast_next_1h_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="virtual_soc_end_tomorrow",
        name="Virtual SoC End Tomorrow",
        value_fn=lambda data: round(_plan(data).virtual_soc_end_tomorrow_pct, 1),
        native_unit_of_measurement=PERCENTAGE,
    ),
    BatteryStrategySensorDescription(
        key="baseline_cost_today",
        name="Baseline Cost Today",
        value_fn=lambda data: (
            _plan(data).daily_costs.get(_today(data)).base_eur
            if _today(data) in _plan(data).daily_costs
            else 0
        ),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="optimized_cost_today",
        name="Optimized Cost Today",
        value_fn=lambda data: (
            _plan(data).daily_costs.get(_today(data)).with_bat_eur
            if _today(data) in _plan(data).daily_costs
            else 0
        ),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="estimated_savings_today",
        name="Estimated Savings Today",
        value_fn=lambda data: (
            _plan(data).daily_costs.get(_today(data)).saving_eur
            if _today(data) in _plan(data).daily_costs
            else 0
        ),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="baseline_cost_tomorrow",
        name="Baseline Cost Tomorrow",
        value_fn=lambda data: (
            _plan(data).daily_costs.get(_tomorrow(data)).base_eur
            if _tomorrow(data) in _plan(data).daily_costs
            else 0
        ),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="optimized_cost_tomorrow",
        name="Optimized Cost Tomorrow",
        value_fn=lambda data: (
            _plan(data).daily_costs.get(_tomorrow(data)).with_bat_eur
            if _tomorrow(data) in _plan(data).daily_costs
            else 0
        ),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="estimated_savings_tomorrow",
        name="Estimated Savings Tomorrow",
        value_fn=lambda data: (
            _plan(data).daily_costs.get(_tomorrow(data)).saving_eur
            if _tomorrow(data) in _plan(data).daily_costs
            else 0
        ),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="actual_savings_today",
        name="Actual Savings Today",
        value_fn=lambda data: round(_raw_float(data, "actual_savings_today_eur"), 3),
        native_unit_of_measurement="EUR",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    BatteryStrategySensorDescription(
        key="actual_savings_cumulative",
        name="Actual Savings Cumulative",
        value_fn=lambda data: round(
            _raw_float(
                data,
                "actual_savings_cumulative_eur",
                _raw_float(data, "actual_savings_lifetime_eur"),
            ),
            3,
        ),
        native_unit_of_measurement="EUR",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    BatteryStrategySensorDescription(
        key="actual_charge_total_today",
        name="Actual Charge Total Today",
        value_fn=_actual_charge_total_kwh,
        native_unit_of_measurement="kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    BatteryStrategySensorDescription(
        key="actual_charge_grid_today",
        name="Actual Charge Grid Today",
        value_fn=lambda data: round(
            _raw_float(data, "actual_battery_charge_grid_today_kwh"), 3
        ),
        native_unit_of_measurement="kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    BatteryStrategySensorDescription(
        key="actual_charge_pv_today",
        name="Actual Charge PV Today",
        value_fn=lambda data: round(
            _raw_float(data, "actual_battery_charge_pv_today_kwh"), 3
        ),
        native_unit_of_measurement="kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    BatteryStrategySensorDescription(
        key="actual_avg_charge_price_today",
        name="Actual Avg Charge Price Today",
        value_fn=_actual_avg_charge_price_ct,
        native_unit_of_measurement="ct/kWh",
    ),
    BatteryStrategySensorDescription(
        key="actual_discharge_credited_today",
        name="Actual Discharge Credited Today",
        value_fn=lambda data: round(
            _raw_float(data, "actual_battery_discharge_credited_today_kwh"), 3
        ),
        native_unit_of_measurement="kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    BatteryStrategySensorDescription(
        key="actual_avg_discharge_price_today",
        name="Actual Avg Discharge Price Today",
        value_fn=_actual_avg_discharge_price_ct,
        native_unit_of_measurement="ct/kWh",
    ),
    BatteryStrategySensorDescription(
        key="profile_today",
        name="Profile Today",
        value_fn=lambda data: len(
            [p for p in _plan(data).points if p.date == _today(data)]
        ),
        attr_fn=lambda data: _profile_attrs(data, _today(data)),
    ),
    BatteryStrategySensorDescription(
        key="profile_tomorrow",
        name="Profile Tomorrow",
        value_fn=lambda data: len(
            [p for p in _plan(data).points if p.date == _tomorrow(data)]
        ),
        attr_fn=lambda data: _profile_attrs(data, _tomorrow(data)),
    ),
    BatteryStrategySensorDescription(
        key="profile_48h",
        name="Profile 48h",
        value_fn=lambda data: len(_plan(data).points),
        attr_fn=lambda data: _profile_attrs(data, None),
    ),
    BatteryStrategySensorDescription(
        key="plan_slots",
        name="Plan Slots",
        value_fn=lambda data: len(_plan(data).points),
        attr_fn=lambda data: _plan_slot_attrs(data),
    ),
)


def _plan_slot_attrs(data):
    """Return a compact, presentation-only view of future optimizer slots."""
    rows = []
    for point in _plan(data).points:
        charge_w = max(0.0, float(point.charge_fc_w))
        pv_surplus_w = max(0.0, float(point.pv_fc_w) - float(point.load_fc_w))
        pv_charge_w = (
            min(charge_w, pv_surplus_w)
            if point.pv_charge_fc_w is None
            else max(0.0, float(point.pv_charge_fc_w))
        )
        grid_charge_w = (
            max(0.0, charge_w - pv_charge_w)
            if point.grid_charge_fc_w is None
            else max(0.0, float(point.grid_charge_fc_w))
        )
        required_charge_w = charge_w if grid_charge_w > 0.0 else 0.0
        if point.required_charge_fc_w is not None:
            required_charge_w = max(0.0, float(point.required_charge_fc_w))
        rows.append(
            [
                int(point.ts_ms // 1000),
                round(float(point.price_ct), 2),
                _slot_energy_kwh(
                    float(point.load_fc_w) - float(point.pv_fc_w), signed=True
                ),
                round(max(0.0, float(point.discharge_budget_kwh)), 3),
                _slot_energy_kwh(point.discharge_fc_w),
                _slot_energy_kwh(charge_w),
                _slot_energy_kwh(pv_charge_w),
                _slot_energy_kwh(grid_charge_w),
                _slot_energy_kwh(required_charge_w),
                round(float(point.soc_pct), 1),
            ]
        )
    return {
        "columns": [
            "slot_start",
            "price_ct_per_kwh",
            "planned_grid_net_before_battery_no_ev_kwh",
            "discharge_budget_kwh",
            "planned_discharge_kwh",
            "planned_charge_kwh",
            "planned_pv_charge_kwh",
            "planned_grid_charge_kwh",
            "required_charge_kwh",
            "planned_soc_pct",
        ],
        "rows": rows,
    }


def _slot_energy_kwh(power_w, *, signed=False):
    """Convert a slot-average power to energy for one 15-minute slot."""
    value = float(power_w) * 0.25 / 1000.0
    return round(value if signed else max(0.0, value), 3)


def _profile_attrs(data, date):
    plan = _plan(data)
    raw = data.get("optimizer_attrs") or {}
    raw_attrs = _raw_profile_attrs(raw, date)
    if raw_attrs is not None:
        return raw_attrs
    return {
        "price": plan.profile("price_ct", date),
        "soc": plan.profile("soc_pct", date),
        "power": plan.profile("power_w", date),
        "charge_power": plan.profile("charge_fc_w", date),
        "discharge_power": plan.profile("discharge_fc_w", date),
        "discharge_budget_kwh": plan.profile("discharge_budget_kwh", date),
        "pv_fc_power": plan.profile("pv_fc_w", date),
        "house_fc_power": plan.profile("load_fc_w", date),
    }


def _raw_profile_attrs(raw: dict, date):
    if not isinstance(raw, dict):
        return None
    if date is None:
        prefix = "profile_48h"
    elif date == dt_util.now().date().isoformat():
        prefix = "profile_today"
    elif date == (dt_util.now().date() + dt.timedelta(days=1)).isoformat():
        prefix = "profile_tomorrow"
    else:
        return None

    if date is None:
        key_map = {
            "pv_fc_power": "pv_fc_power",
            "house_fc_power": "house_fc_power",
            "pv_actual_power": "pv_actual_power",
            "house_actual_power": "house_actual_power",
            "grid_net_actual_power": "grid_net_actual_power",
        }
    elif date == dt_util.now().date().isoformat():
        key_map = {
            "price": "price",
            "soc": "soc",
            "pv_actual_power": "pv_actual_power",
            "house_actual_power": "house_actual_power",
        }
    else:
        key_map = {"price": "price", "soc": "soc"}
    attrs = {
        name: _profile(raw.get(f"{prefix}_{raw_key}"))
        for name, raw_key in key_map.items()
    }
    if any(attrs.values()):
        return attrs
    return None


def _profile(raw):
    out = []
    for item in raw or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            out.append([int(float(item[0])), float(item[1])])
        except (TypeError, ValueError):
            continue
    return out


def _format_ts_ms(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    value = dt.datetime.fromtimestamp(ts_ms / 1000.0, dt.timezone.utc)
    return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M")


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up Battery Strategy sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BatteryStrategySensor(coordinator, description) for description in SENSORS
    )


class BatteryStrategySensor(BatteryStrategyEntity, SensorEntity):
    """Battery Strategy sensor backed by the coordinator."""

    # Plan table rows are current presentation data, not history. Keeping them
    # out of Recorder avoids duplicating the full planning horizon every time
    # the optimizer publishes a revised plan.
    _unrecorded_attributes = frozenset({"columns", "rows"})

    def __init__(self, coordinator, description: BatteryStrategySensorDescription):
        """Initialize sensor."""
        super().__init__(coordinator, description.key, description.name)
        self.entity_description = description
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class

    @property
    def state_class(self):
        """Return the state class explicitly for statistics validation."""
        return self.entity_description.state_class

    @property
    def native_value(self):
        """Return current value."""
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        """Return profile attributes for chart sensors."""
        if not self.coordinator.data or self.entity_description.attr_fn is None:
            return None
        return self.entity_description.attr_fn(self.coordinator.data)

    async def async_added_to_hass(self) -> None:
        """Write the current coordinator value immediately after registration."""
        await super().async_added_to_hass()
        self.async_write_ha_state()

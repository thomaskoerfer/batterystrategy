"""Sensors for Battery Strategy read-only parallel operation."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from typing import Callable
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import UnitOfPower
try:
    from homeassistant.const import PERCENTAGE
except ImportError:  # pragma: no cover - compatibility with older HA versions.
    from homeassistant.const import PERCENT as PERCENTAGE

from .const import DOMAIN
from .entity import BatteryStrategyEntity

LOCAL_TZ = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True, kw_only=True)
class BatteryStrategySensorDescription(SensorEntityDescription):
    """Description for a Battery Strategy sensor."""

    value_fn: Callable[[object], object]
    attr_fn: Callable[[object], dict] | None = None


def _command(data):
    return data["command"]


def _inputs(data):
    return data["inputs"]


def _parallel(data):
    return data["parallel"]


def _plan(data):
    return data["plan"]


def _plan_comparison(data):
    return data["plan_comparison"]


def _plan_to_live(data):
    return data["plan_to_live"]




def _raw_float(data, key: str, default: float = 0.0) -> float:
    try:
        return float((data.get("optimizer_attrs") or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _actual_charge_total_kwh(data) -> float:
    return round(_raw_float(data, "actual_battery_charge_grid_today_kwh") + _raw_float(data, "actual_battery_charge_pv_today_kwh"), 3)


def _actual_avg_charge_price_ct(data) -> float:
    kwh = _actual_charge_total_kwh(data)
    if kwh <= 0.01:
        return 0.0
    return round((_raw_float(data, "actual_battery_charge_cost_today_eur") / kwh) * 100.0, 1)


def _actual_avg_discharge_price_ct(data) -> float:
    kwh = _raw_float(data, "actual_battery_discharge_credited_today_kwh")
    if kwh <= 0.01:
        return 0.0
    return round((_raw_float(data, "actual_battery_discharge_credit_today_eur") / kwh) * 100.0, 1)

def _today(data):
    return dt.datetime.now(LOCAL_TZ).date().isoformat()


def _tomorrow(data):
    return (dt.datetime.now(LOCAL_TZ).date() + dt.timedelta(days=1)).isoformat()


SENSORS: tuple[BatteryStrategySensorDescription, ...] = (
    BatteryStrategySensorDescription(key="mode", name="Mode", value_fn=lambda data: _command(data).mode),
    BatteryStrategySensorDescription(
        key="command_power",
        name="Command Power",
        value_fn=lambda data: _command(data).power_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(key="reason", name="Reason", value_fn=lambda data: _command(data).reason),
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
        native_unit_of_measurement=PERCENTAGE,
    ),
    BatteryStrategySensorDescription(
        key="parallel_samples",
        name="Parallel Samples",
        value_fn=lambda data: _parallel(data).samples,
    ),
    BatteryStrategySensorDescription(
        key="parallel_mode_match",
        name="Parallel Mode Match",
        value_fn=lambda data: round(_parallel(data).mode_match_ratio * 100.0, 1),
        native_unit_of_measurement=PERCENTAGE,
    ),
    BatteryStrategySensorDescription(
        key="parallel_max_power_delta",
        name="Parallel Max Power Delta",
        value_fn=lambda data: _parallel(data).max_power_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="parallel_passed",
        name="Parallel Passed",
        value_fn=lambda data: "on" if _parallel(data).passed else "off",
    ),
    BatteryStrategySensorDescription(
        key="parallel_input_samples",
        name="Parallel Input Samples",
        value_fn=lambda data: _parallel(data).input_samples,
    ),
    BatteryStrategySensorDescription(
        key="parallel_command_passed",
        name="Parallel Command Passed",
        value_fn=lambda data: "on" if _parallel(data).command_passed else "off",
    ),
    BatteryStrategySensorDescription(
        key="parallel_max_house_load_no_ev_delta",
        name="Parallel Max House Load No EV Delta",
        value_fn=lambda data: _parallel(data).max_house_load_no_ev_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="parallel_max_house_load_total_delta",
        name="Parallel Max House Load Total Delta",
        value_fn=lambda data: _parallel(data).max_house_load_total_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="parallel_max_pv_delta",
        name="Parallel Max PV Delta",
        value_fn=lambda data: _parallel(data).max_pv_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="parallel_max_residual_no_ev_delta",
        name="Parallel Max Residual No EV Delta",
        value_fn=lambda data: _parallel(data).max_residual_no_ev_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="parallel_max_residual_with_ev_delta",
        name="Parallel Max Residual With EV Delta",
        value_fn=lambda data: _parallel(data).max_residual_with_ev_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
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
        value_fn=lambda data: _plan(data).current_power_w if _plan(data).current_mode == "input" else 0,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="planned_discharge_power",
        name="Planned Discharge Power",
        value_fn=lambda data: _plan(data).current_power_w if _plan(data).current_mode == "output" else 0,
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
        value_fn=lambda data: "on" if _plan_to_live(data).grid_charge_allowed else "off",
    ),
    BatteryStrategySensorDescription(
        key="plan_live_discharge_budget",
        name="Discharge Budget",
        value_fn=lambda data: _plan_to_live(data).discharge_budget_kwh,
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
        value_fn=lambda data: (_plan(data).daily_costs.get(_today(data)).base_eur if _today(data) in _plan(data).daily_costs else 0),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="optimized_cost_today",
        name="Optimized Cost Today",
        value_fn=lambda data: (_plan(data).daily_costs.get(_today(data)).with_bat_eur if _today(data) in _plan(data).daily_costs else 0),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="estimated_savings_today",
        name="Estimated Savings Today",
        value_fn=lambda data: (_plan(data).daily_costs.get(_today(data)).saving_eur if _today(data) in _plan(data).daily_costs else 0),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="baseline_cost_tomorrow",
        name="Baseline Cost Tomorrow",
        value_fn=lambda data: (_plan(data).daily_costs.get(_tomorrow(data)).base_eur if _tomorrow(data) in _plan(data).daily_costs else 0),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="optimized_cost_tomorrow",
        name="Optimized Cost Tomorrow",
        value_fn=lambda data: (_plan(data).daily_costs.get(_tomorrow(data)).with_bat_eur if _tomorrow(data) in _plan(data).daily_costs else 0),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="estimated_savings_tomorrow",
        name="Estimated Savings Tomorrow",
        value_fn=lambda data: (_plan(data).daily_costs.get(_tomorrow(data)).saving_eur if _tomorrow(data) in _plan(data).daily_costs else 0),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="actual_savings_today",
        name="Actual Savings Today",
        value_fn=lambda data: round(_raw_float(data, "actual_savings_today_eur"), 3),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="actual_savings_cumulative",
        name="Actual Savings Cumulative",
        value_fn=lambda data: round(_raw_float(data, "actual_savings_cumulative_eur", _raw_float(data, "actual_savings_lifetime_eur")), 3),
        native_unit_of_measurement="EUR",
    ),
    BatteryStrategySensorDescription(
        key="actual_charge_total_today",
        name="Actual Charge Total Today",
        value_fn=_actual_charge_total_kwh,
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="actual_charge_grid_today",
        name="Actual Charge Grid Today",
        value_fn=lambda data: round(_raw_float(data, "actual_battery_charge_grid_today_kwh"), 3),
        native_unit_of_measurement="kWh",
    ),
    BatteryStrategySensorDescription(
        key="actual_charge_pv_today",
        name="Actual Charge PV Today",
        value_fn=lambda data: round(_raw_float(data, "actual_battery_charge_pv_today_kwh"), 3),
        native_unit_of_measurement="kWh",
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
        value_fn=lambda data: round(_raw_float(data, "actual_battery_discharge_credited_today_kwh"), 3),
        native_unit_of_measurement="kWh",
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
        value_fn=lambda data: len([p for p in _plan(data).points if p.date == _today(data)]),
        attr_fn=lambda data: _profile_attrs(data, _today(data)),
    ),
    BatteryStrategySensorDescription(
        key="profile_tomorrow",
        name="Profile Tomorrow",
        value_fn=lambda data: len([p for p in _plan(data).points if p.date == _tomorrow(data)]),
        attr_fn=lambda data: _profile_attrs(data, _tomorrow(data)),
    ),
    BatteryStrategySensorDescription(
        key="profile_48h",
        name="Profile 48h",
        value_fn=lambda data: len(_plan(data).points),
        attr_fn=lambda data: _profile_attrs(data, None),
    ),
    BatteryStrategySensorDescription(
        key="plan_input_passed",
        name="Plan Input Passed",
        value_fn=lambda data: "on" if _plan_comparison(data).plan_input_passed else "off",
    ),
    BatteryStrategySensorDescription(
        key="tomorrow_strategy_passed",
        name="Tomorrow Strategy Passed",
        value_fn=lambda data: "on" if _plan_comparison(data).tomorrow_strategy_passed else "off",
    ),
    BatteryStrategySensorDescription(
        key="forty8h_strategy_passed",
        name="48h Strategy Passed",
        value_fn=lambda data: "on" if _plan_comparison(data).forty8h_strategy_passed else "off",
    ),
    BatteryStrategySensorDescription(
        key="live_command_passed",
        name="Live Command Passed",
        value_fn=lambda data: "on" if _plan_comparison(data).live_command_passed else "off",
    ),
    BatteryStrategySensorDescription(
        key="override_active",
        name="Override Active",
        value_fn=lambda data: "on" if _plan_comparison(data).override_active else "off",
    ),
    BatteryStrategySensorDescription(
        key="plan_max_tomorrow_power_delta",
        name="Plan Max Tomorrow Power Delta",
        value_fn=lambda data: _plan_comparison(data).max_tomorrow_power_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    BatteryStrategySensorDescription(
        key="plan_max_48h_power_delta",
        name="Plan Max 48h Power Delta",
        value_fn=lambda data: _plan_comparison(data).max_48h_power_delta_w,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
)


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
    elif date == dt.datetime.now(LOCAL_TZ).date().isoformat():
        prefix = "profile_today"
    elif date == (dt.datetime.now(LOCAL_TZ).date() + dt.timedelta(days=1)).isoformat():
        prefix = "profile_tomorrow"
    else:
        return None

    key_map = {
        "price": "price",
        "soc": "soc",
        "power": "power",
        "charge_power": "charge_power",
        "discharge_power": "discharge_power",
        "discharge_budget_kwh": "discharge_budget_kwh",
        "pv_fc_power": "pv_fc_power",
        "house_fc_power": "house_fc_power",
        "grid_import_fc_power": "grid_import_fc_power",
        "grid_export_fc_power": "grid_export_fc_power",
        "grid_net_fc_power": "grid_net_fc_power",
        "pv_actual_power": "pv_actual_power",
        "house_actual_power": "house_actual_power",
        "grid_net_actual_power": "grid_net_actual_power",
    }
    attrs = {name: _profile(raw.get(f"{prefix}_{raw_key}")) for name, raw_key in key_map.items()}
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
    return dt.datetime.fromtimestamp(ts_ms / 1000.0, LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up Battery Strategy sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BatteryStrategySensor(coordinator, entry.entry_id, description)
        for description in SENSORS
    )


class BatteryStrategySensor(BatteryStrategyEntity, SensorEntity):
    """Battery Strategy sensor backed by the coordinator."""

    def __init__(self, coordinator, entry_id: str, description: BatteryStrategySensorDescription):
        """Initialize sensor."""
        super().__init__(coordinator, description.key, description.name)
        self.entity_description = description
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement

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

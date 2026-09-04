"""Sensors for Battery Strategy."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfPower

from .entity import BatteryStrategyEntity
from .operator_projection import PROFILE_ATTRIBUTE_KEYS, OperatorProjection

STATE_CLASS_MEASUREMENT = SensorStateClass.MEASUREMENT


@dataclass(frozen=True, kw_only=True)
class BatteryStrategySensorDescription(SensorEntityDescription):
    """Description for a precomputed Battery Strategy sensor."""


def _sensor(
    key: str,
    name: str,
    unit: str | None = None,
    *,
    state_class: SensorStateClass | None = None,
) -> BatteryStrategySensorDescription:
    return BatteryStrategySensorDescription(
        key=key,
        name=name,
        native_unit_of_measurement=unit,
        state_class=state_class,
    )


SENSORS: tuple[BatteryStrategySensorDescription, ...] = (
    _sensor("mode", "Mode"),
    _sensor("command_power", "Command Power", UnitOfPower.WATT),
    _sensor("command_source", "Command Source"),
    _sensor("reason", "Reason"),
    _sensor("residual_with_ev", "Residual With EV", UnitOfPower.WATT),
    _sensor("residual_no_ev", "Residual No EV", UnitOfPower.WATT),
    _sensor("pv_surplus", "PV Surplus", UnitOfPower.WATT),
    _sensor("allowed_discharge_load", "Allowed Discharge Load", UnitOfPower.WATT),
    _sensor("house_load_total", "House Load Total", UnitOfPower.WATT),
    _sensor("house_load_no_ev", "House Load No EV", UnitOfPower.WATT),
    _sensor("grid_import", "Grid Import", UnitOfPower.WATT),
    _sensor("grid_export", "Grid Export", UnitOfPower.WATT),
    _sensor("battery_power", "Battery Power", UnitOfPower.WATT),
    _sensor("ev_power", "EV Power", UnitOfPower.WATT),
    _sensor("soc", "SoC", PERCENTAGE),
    _sensor("planned_mode", "Planned Mode"),
    _sensor("planned_power", "Planned Power", UnitOfPower.WATT),
    _sensor("planned_charge_power", "Planned Charge Power", UnitOfPower.WATT),
    _sensor("planned_discharge_power", "Planned Discharge Power", UnitOfPower.WATT),
    _sensor("plan_live_slot_start", "Slot Start"),
    _sensor("plan_live_slot_end", "Slot End"),
    _sensor("plan_live_pv_charge_allowed", "PV Charge Allowed"),
    _sensor("plan_live_must_charge", "Must Charge", UnitOfPower.WATT),
    _sensor("plan_live_must_charge_remaining", "Must Charge Remaining", "kWh"),
    _sensor("plan_live_grid_charge_allowed", "Grid Charge Allowed"),
    _sensor("plan_live_discharge_budget", "Live Remaining Discharge Budget", "kWh"),
    _sensor("optimizer_discharge_budget", "Optimizer Discharge Budget", "kWh"),
    _sensor("load_forecast_next_1h", "Load Forecast Next 1h", "kWh"),
    _sensor("pv_forecast_corrected_next_1h", "PV Forecast Corrected Next 1h", "kWh"),
    _sensor("net_load_forecast_next_1h", "Net Load Forecast Next 1h", "kWh"),
    _sensor("grid_import_forecast_next_1h", "Grid Import Forecast Next 1h", "kWh"),
    _sensor("grid_export_forecast_next_1h", "Grid Export Forecast Next 1h", "kWh"),
    _sensor("virtual_soc_end_tomorrow", "Virtual SoC End Tomorrow", PERCENTAGE),
    _sensor("baseline_cost_today", "Baseline Cost Today", "EUR"),
    _sensor("optimized_cost_today", "Optimized Cost Today", "EUR"),
    _sensor("estimated_savings_today", "Estimated Savings Today", "EUR"),
    _sensor("baseline_cost_tomorrow", "Baseline Cost Tomorrow", "EUR"),
    _sensor("optimized_cost_tomorrow", "Optimized Cost Tomorrow", "EUR"),
    _sensor("estimated_savings_tomorrow", "Estimated Savings Tomorrow", "EUR"),
    _sensor(
        "actual_savings_today",
        "Actual Savings Today",
        "EUR",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    _sensor(
        "actual_savings_cumulative",
        "Actual Savings Cumulative",
        "EUR",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    _sensor(
        "actual_charge_total_today",
        "Actual Charge Total Today",
        "kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    _sensor(
        "actual_charge_grid_today",
        "Actual Charge Grid Today",
        "kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    _sensor(
        "actual_charge_pv_today",
        "Actual Charge PV Today",
        "kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    _sensor("actual_avg_charge_price_today", "Actual Avg Charge Price Today", "ct/kWh"),
    _sensor(
        "actual_discharge_credited_today",
        "Actual Discharge Credited Today",
        "kWh",
        state_class=STATE_CLASS_MEASUREMENT,
    ),
    _sensor(
        "actual_avg_discharge_price_today",
        "Actual Avg Discharge Price Today",
        "ct/kWh",
    ),
    _sensor("profile_today", "Profile Today"),
    _sensor("profile_tomorrow", "Profile Tomorrow"),
    _sensor("profile_48h", "Profile 48h"),
    _sensor("plan_slots", "Plan Slots"),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up Battery Strategy sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        BatteryStrategySensor(coordinator, description) for description in SENSORS
    )


class BatteryStrategySensor(BatteryStrategyEntity, SensorEntity):
    """Battery Strategy sensor backed by a precomputed projection."""

    _unrecorded_attributes = PROFILE_ATTRIBUTE_KEYS

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
        """Return one value computed during the coordinator refresh."""
        projection = self._projection()
        return projection.value(self.entity_description.key) if projection else None

    @property
    def extra_state_attributes(self):
        """Return attributes computed during the coordinator refresh."""
        projection = self._projection()
        return projection.attrs(self.entity_description.key) if projection else None

    def _projection(self) -> OperatorProjection | None:
        data = self.coordinator.data
        if not data:
            return None
        projection = data.get("operator_projection")
        return projection if isinstance(projection, OperatorProjection) else None

    async def async_added_to_hass(self) -> None:
        """Write the current coordinator value immediately after registration."""
        await super().async_added_to_hass()
        self.async_write_ha_state()

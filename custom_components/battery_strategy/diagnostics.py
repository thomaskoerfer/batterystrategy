"""Diagnostics support for Battery Strategy."""

from __future__ import annotations

from dataclasses import asdict

from homeassistant.components.diagnostics import async_redact_data

from .const import DOMAIN

TO_REDACT = {
    "signed_grid_power_entity",
    "grid_l1_entity",
    "grid_l2_entity",
    "grid_l3_entity",
    "grid_import_entity",
    "grid_export_entity",
    "pv_power_entity",
    "price_entity",
    "battery_soc_entity",
    "battery_power_entity",
    "battery_input_energy_entity",
    "battery_output_energy_entity",
    "ev_power_entity",
    "zendure_ac_mode_entity",
    "zendure_output_pack_power_entity",
    "zendure_pack_input_power_entity",
    "zendure_output_home_power_entity",
    "zendure_grid_input_power_entity",
    "zendure_input_limit_entity",
    "zendure_output_limit_entity",
}


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    """Return bounded, privacy-safe diagnostics for one config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}
    plan = data.get("plan")
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "inputs": asdict(data["inputs"]) if data.get("inputs") else None,
        "command": asdict(data["command"]) if data.get("command") else None,
        "plan_to_live": asdict(data["plan_to_live"])
        if data.get("plan_to_live")
        else None,
        "plan": {
            "current_mode": getattr(plan, "current_mode", None),
            "current_power_w": getattr(plan, "current_power_w", None),
            "reason": getattr(plan, "reason", None),
            "point_count": len(getattr(plan, "points", [])),
        },
        "actuation": data.get("actuation"),
        "optimizer_age_s": data.get("optimizer_age_s"),
        "soc_control_ready": data.get("soc_control_ready"),
        "soc_estimate_stale": data.get("soc_estimate_stale"),
        "forecast": {
            key.removeprefix("forecast_"): value
            for key, value in (data.get("optimizer_attrs") or {}).items()
            if key
            in {
                "forecast_source",
                "forecast_slot_count",
                "forecast_runtime_ms",
                "forecast_model_version",
            }
        },
        "feature_store": data.get("feature_store"),
        "forecast_shadow": {
            key.removeprefix("forecast_shadow_"): value
            for key, value in (data.get("optimizer_attrs") or {}).items()
            if key.startswith("forecast_shadow_")
        },
        "forecast_shadow_environment": data.get("forecast_shadow_environment"),
        "strategy_enabled": data.get("strategy_enabled"),
    }

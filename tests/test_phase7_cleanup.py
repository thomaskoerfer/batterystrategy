"""Phase-7 regression guards for removed transitional implementations."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.battery_strategy import planning_adapter, planning_pipeline
from custom_components.battery_strategy.models import StrategyOptions
from custom_components.battery_strategy.planning_runtime import (
    HistoryRole,
    PlanningHistory,
    PlanningObservations,
)
from custom_components.battery_strategy.planning_state import PlanningStateStore
from custom_components.battery_strategy.runtime_market_data import (
    TariffInterval,
    TariffSchedule,
)
from tests.live_contract_helpers import measurements
from tests.planning_runtime_helpers import runtime_snapshot, settings_from_values

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "battery_strategy"


def test_completed_shadow_and_legacy_optimizer_modules_are_absent():
    obsolete = (
        "optimizer_shadow.py",
        "forecast_shadow_runner.py",
        "forecast_shadow_store.py",
        "forecasting/shadow.py",
        "compiler_evaluation.py",
        "optimizer_engine.py",
        "plan_compiler_adapter.py",
    )
    assert all(not (PACKAGE / name).exists() for name in obsolete)


def test_optimizer_orchestration_has_no_recorder_schema_dependency():
    source = (PACKAGE / "planning_pipeline.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "sqlalchemy",
        "states_meta",
        "select s.state",
        "db_engine",
        "build_virtual_plan",
        "optimizer_shadow",
        "urlopen",
        "open_meteo_url",
    )
    assert all(token not in source for token in forbidden)


def test_planning_pipeline_uses_owned_application_boundaries_without_facades():
    source = (PACKAGE / "planning_pipeline.py").read_text(encoding="utf-8")
    forbidden_definitions = (
        "def build_authoritative_plan(",
        "def build_commercial_plan_metadata(",
        "def apply_eex_proxy_prices(",
        "def compute_price_quantiles(",
        "def classify_discharge_mode(",
        "def adapt_pure_optimizer_plan(",
        "def eex_filter_rows(",
        "def eex_fetch_settlement(",
        "def build_eex_proxy_day_prices(",
        "def indexed_value_at_or_before(",
    )
    assert all(token not in source for token in forbidden_definitions)
    assert "_planning_service(settings).plan(" in source
    assert "global " not in source
    assert "_RUNTIME_" not in source
    assert "def _configure(" not in source
    assert "market_context.apply_eex_proxy_prices(" in source
    assert "_update_actual_savings(" in source
    assert "startsAt" not in source
    assert "state_schema" not in source


def test_planning_runtime_snapshots_are_immutable_and_isolated():
    first = runtime_snapshot(
        settings=settings_from_values(
            timezone="Europe/Berlin", battery_capacity_kwh=6.0
        ),
        observations=PlanningObservations(
            current_price_ct_per_kwh=20.0,
            future_max_price_ct_per_kwh=30.0,
            grid_import_w=100.0,
            grid_export_w=0.0,
            pv_generation_w=0.0,
            battery_charge_w=0.0,
            battery_discharge_w=0.0,
            battery_soc_pct=42.0,
            battery_min_soc_pct=5.0,
            ev_charge_w=0.0,
            heat_pump_power_w=0.0,
            pv_next_hour_kwh=0.0,
            pv_tomorrow_kwh=None,
            cloud_cover_pct=50.0,
            shortwave_radiation_w_m2=0.0,
        ),
    )
    second = runtime_snapshot(
        settings=settings_from_values(timezone="UTC", battery_capacity_kwh=10.0),
        observations=PlanningObservations(
            current_price_ct_per_kwh=20.0,
            future_max_price_ct_per_kwh=30.0,
            grid_import_w=100.0,
            grid_export_w=0.0,
            pv_generation_w=0.0,
            battery_charge_w=0.0,
            battery_discharge_w=0.0,
            battery_soc_pct=81.0,
            battery_min_soc_pct=5.0,
            ev_charge_w=0.0,
            heat_pump_power_w=0.0,
            pv_next_hour_kwh=0.0,
            pv_tomorrow_kwh=None,
            cloud_cover_pct=50.0,
            shortwave_radiation_w_m2=0.0,
        ),
    )

    assert first.settings.battery_capacity_kwh == 6.0
    assert second.settings.battery_capacity_kwh == 10.0
    assert first.observations.battery_soc_pct == 42.0
    assert second.observations.battery_soc_pct == 81.0
    with pytest.raises((AttributeError, TypeError)):
        first.observations.battery_soc_pct = 50.0


def test_planning_snapshot_excludes_adapter_and_persistence_details():
    runtime_source = (PACKAGE / "planning_runtime.py").read_text(encoding="utf-8")
    pipeline_source = (PACKAGE / "planning_pipeline.py").read_text(encoding="utf-8")

    forbidden_runtime_fields = (
        "states:",
        "entity_map:",
        "entity_scale:",
        "price_intervals:",
        "history_series:",
        "state_store:",
        "config_dir:",
        "state_file:",
    )
    assert all(token not in runtime_source for token in forbidden_runtime_fields)
    assert "PlanningRuntime.from_mapping" not in runtime_source
    assert "def collect_inputs(" not in pipeline_source
    assert "owner_state: PlanningOwnerState" in pipeline_source
    coordinator_source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
    assert 'LOGGER.warning("Planning snapshot capture failed: %s", err)' in (
        coordinator_source
    )


def test_runtime_settings_preserve_established_zero_value_fallbacks():
    settings = settings_from_values(
        battery_capacity_kwh=0.0,
        max_soc_pct=0.0,
        max_charge_power_w=0.0,
        max_discharge_power_w=0.0,
        round_trip_efficiency=0.0,
        planning_horizon_h=0,
    )

    assert settings.battery_capacity_kwh == 6.0
    assert settings.max_soc_pct == 100.0
    assert settings.max_charge_power_w == 2400.0
    assert settings.max_discharge_power_w == 2400.0
    assert settings.round_trip_efficiency == 0.8
    assert settings.planning_horizon_h == 48


def test_future_zero_and_negative_prices_are_preserved():
    start = dt.datetime(2027, 1, 15, 10, 15, tzinfo=dt.timezone.utc)
    tariffs = TariffSchedule.from_provider_rows(
        [
            {"start_time": start.isoformat(), "price": -0.10},
            {
                "start_time": (start + dt.timedelta(minutes=15)).isoformat(),
                "price": 0.0,
            },
        ],
        ZoneInfo("UTC"),
    )

    assert tariffs.future_price_stats(start) == {"min_ct": -10.0, "max_ct": 0.0}


def test_planning_runtime_detaches_mutable_provider_values():
    provider_rows = [
        {"start_time": "2027-01-15T10:00:00+00:00", "price": 0.10}
    ]
    runtime = runtime_snapshot(
        provider_prices=provider_rows,
        history_series={HistoryRole.PV_GENERATION_POWER_W: [(100.0, 1250.0)]},
    )
    provider_rows[0]["price"] = 0.99

    assert runtime.tariffs.intervals[0].price_eur_per_kwh == 0.10
    assert runtime.history.read([HistoryRole.PV_GENERATION_POWER_W], 0.0) == {
        HistoryRole.PV_GENERATION_POWER_W: [(100.0, 1250.0)]
    }


def test_direct_snapshot_construction_normalizes_mutable_containers():
    interval = TariffInterval(
        dt.datetime(2027, 1, 15, 10, tzinfo=dt.timezone.utc), 0.20
    )
    interval_values = [interval]
    schedule = TariffSchedule(interval_values)
    series_values = [(100.0, 1250.0)]
    history = PlanningHistory(
        {HistoryRole.PV_GENERATION_POWER_W: series_values}
    )
    runtime = runtime_snapshot()
    replaced = replace(runtime, forecast_weather=[])

    interval_values.clear()
    series_values.clear()
    assert schedule.intervals == (interval,)
    assert history.read([HistoryRole.PV_GENERATION_POWER_W], 0.0)[
        HistoryRole.PV_GENERATION_POWER_W
    ]
    assert replaced.forecast_weather == ()


def test_provider_source_metadata_cannot_change_tariff_authority():
    schedule = TariffSchedule.from_provider_rows(
        [
            {
                "start_time": "2027-01-15T10:00:00+00:00",
                "price": 0.20,
                "source": "untrusted-provider-label",
            }
        ],
        ZoneInfo("UTC"),
    )

    assert schedule.intervals[0].source == "tibber"


def test_tariff_timestamps_are_normalized_to_home_assistant_timezone():
    timezone = ZoneInfo("Europe/Berlin")
    schedule = TariffSchedule.from_provider_rows(
        [{"start_time": "2027-01-14T23:15:00+00:00", "price": 0.20}],
        timezone,
    )

    assert schedule.intervals[0].starts_at.isoformat() == "2027-01-15T00:15:00+01:00"
    assert len(schedule.for_dates({"2027-01-15"})) == 1


def test_non_finite_prices_and_observations_are_rejected():
    schedule = TariffSchedule.from_provider_rows(
        [{"start_time": "2027-01-15T10:00:00+00:00", "price": "nan"}],
        ZoneInfo("UTC"),
    )
    assert schedule.intervals == ()

    with pytest.raises(ValueError, match="finite"):
        replace(runtime_snapshot().observations, grid_import_w=float("nan"))


def test_captured_time_selects_the_exact_quarter_boundary_price():
    boundary = dt.datetime(2027, 1, 15, 10, 15, tzinfo=dt.timezone.utc)
    tariffs = TariffSchedule.from_provider_rows(
        [
            {"start_time": "2027-01-15T10:00:00+00:00", "price": 0.10},
            {"start_time": "2027-01-15T10:15:00+00:00", "price": 0.20},
        ],
        ZoneInfo("UTC"),
    )

    assert tariffs.price_eur_at(boundary.timestamp()) == 0.20


def test_planning_path_has_no_wall_clock_after_adapter_capture():
    paths = (
        "planning_pipeline.py",
        "planning_runtime.py",
        "planning_state.py",
        "planning_result.py",
        "runtime_measurements.py",
        "forecast_application.py",
        "planning_service.py",
        "plan_presentation.py",
    )
    source = "\n".join((PACKAGE / path).read_text(encoding="utf-8") for path in paths)

    assert "datetime.now(" not in source
    assert "dt.datetime.now(" not in source
    assert "time.time(" not in source


def test_stale_planning_result_cannot_replace_adapter_cache(monkeypatch, tmp_path):
    adapter = planning_adapter.PlanningPipelineAdapter(
        state_store=PlanningStateStore(str(tmp_path / "state.json"))
    )
    options = StrategyOptions()
    previous = planning_adapter.result_from_persisted_output(
        {"timestamp": "2027-01-15T10:15:00+00:00"},
        options,
        timezone=ZoneInfo("Europe/Berlin"),
        now_ms=1_800_000_000_000,
    )
    adapter._last_result = previous
    adapter._last_output = {"timestamp": "2027-01-15T10:15:00+00:00"}
    adapter._last_options = options

    def stale(_runtime, _state):
        raise planning_pipeline.StalePlanningResult

    monkeypatch.setattr(planning_pipeline, "run", stale)
    monkeypatch.setattr(planning_adapter.time, "monotonic", lambda: 123.0)
    result = adapter.run(
        measurements(0, 0, 0, 0, 0, 50),
        options,
        force=True,
        runtime_context=planning_adapter.PlanningCapture(
            runtime_snapshot(), {}, {}
        ),
    )

    assert result is previous
    assert adapter._last_output == {"timestamp": "2027-01-15T10:15:00+00:00"}
    assert adapter._last_run_monotonic == 123.0


def test_stale_planning_result_records_changed_options_without_retry_loop(
    monkeypatch, tmp_path
):
    adapter = planning_adapter.PlanningPipelineAdapter(
        state_store=PlanningStateStore(str(tmp_path / "state.json"))
    )
    old_options = StrategyOptions()
    new_options = StrategyOptions(grid_charging="price_sensitive")
    previous = planning_adapter.result_from_persisted_output(
        {"timestamp": "2027-01-15T10:15:00+00:00"},
        old_options,
        timezone=ZoneInfo("Europe/Berlin"),
        now_ms=1_800_000_000_000,
    )
    adapter._last_result = previous
    adapter._last_output = {"timestamp": "2027-01-15T10:15:00+00:00"}
    adapter._last_options = old_options

    def stale(_runtime, _state):
        raise planning_pipeline.StalePlanningResult

    monkeypatch.setattr(planning_pipeline, "run", stale)
    monkeypatch.setattr(planning_adapter.time, "monotonic", lambda: 123.0)
    result = adapter.run(
        measurements(0, 0, 0, 0, 0, 50),
        new_options,
        force=True,
        runtime_context=planning_adapter.PlanningCapture(
            runtime_snapshot(), {}, {}
        ),
    )

    assert result.battery_plan is None
    assert adapter._last_result is result
    assert adapter._last_options == new_options
    assert adapter.needs_run(new_options) is False


def test_planning_service_does_not_duplicate_optimizer_version():
    source = (PACKAGE / "planning_service.py").read_text(encoding="utf-8")
    assert '"economic-dp-v1"' not in source
    assert '"optimizer_source": OPTIMIZER_VERSION' in source


def test_coordinator_has_one_authoritative_plan_compiler_path():
    source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
    runtime = (PACKAGE / "compiler_runtime.py").read_text(encoding="utf-8")
    forbidden = (
        "plan_live_directive_from_plan",
        "_directive_with_progress",
        "_evaluate_compiler_shadow",
        "COMPILER_SHADOW_TRACE_FILE",
    )
    assert all(token not in source for token in forbidden)
    assert "DeterministicPlanCompiler()" not in source
    assert runtime.count("DeterministicPlanCompiler()") == 1
    strategy = (PACKAGE / "strategy.py").read_text(encoding="utf-8")
    assert "def plan_live_directive_from_plan" not in strategy
    assert "def live_command_from_plan" not in strategy


def test_compiler_path_never_reconstructs_canonical_plan_from_operator_profiles():
    package_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")
    )
    coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")

    assert "contract_plan_from_strategy_plan" not in package_sources
    assert "planning_result.battery_plan" in coordinator


def test_coordinator_preserves_runtime_account_compile_persist_order():
    source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
    runtime = (PACKAGE / "compiler_runtime.py").read_text(encoding="utf-8")
    update = source[source.index("async def _async_update_data") :]
    positions = [
        update.index("self._compiler_runtime.account("),
        update.index("self._compiler_runtime.compile("),
        update.index("await self._async_persist_compiler_runtime("),
        update.index("self._live_controller.command("),
        update.index("await self._async_apply_command("),
    ]
    assert positions == sorted(positions)
    assert "self._compiler_runtime.sync_slot(" not in update
    compile_method = runtime[runtime.index("    def compile(") :]
    assert compile_method.index("self.sync_slot(") < compile_method.index(
        "self._compiler.compile("
    )


def test_actuator_is_the_only_hardware_service_writer():
    coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
    actuator = (PACKAGE / "actuator.py").read_text(encoding="utf-8")
    assert "services.async_call" not in coordinator
    assert "services.async_call" in actuator


def test_home_assistant_runtime_uses_config_entry_ownership():
    package_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")
    )
    integration = (PACKAGE / "__init__.py").read_text(encoding="utf-8")

    assert "hass.data" not in package_sources
    assert "entry.runtime_data = coordinator" in integration
    assert "async def async_setup(" in integration
    assert "_async_register_services(hass)" in integration


def test_sensor_entities_only_read_precomputed_operator_projection():
    sensor = (PACKAGE / "sensor.py").read_text(encoding="utf-8")
    projection = (PACKAGE / "operator_projection.py").read_text(encoding="utf-8")

    assert "build_operator_projection" not in sensor
    assert "dt_util" not in sensor
    assert 'data.get("operator_projection")' in sensor
    assert "PROFILE_ATTRIBUTE_KEYS" in sensor
    assert "def build_operator_projection(" in projection


def test_concrete_actuator_implements_only_the_generic_command_port():
    actuator = (PACKAGE / "actuator.py").read_text(encoding="utf-8")

    assert (
        "async def apply(self, command: BatteryCommand) -> ActuationResult:" in actuator
    )
    assert "async def zero(" not in actuator
    assert "async def failsafe_zero_once(" not in actuator
    assert "StrategyCommand" not in actuator


def test_live_runtime_has_one_contract_model_per_seam():
    production = "\n".join(
        path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")
    )
    models = (PACKAGE / "models.py").read_text(encoding="utf-8")
    plan_models = (PACKAGE / "plan_models.py").read_text(encoding="utf-8")
    coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")

    assert "class StrategyInputs" not in models
    assert "class StrategyCommand" not in models
    assert "class PlanLiveDirective" not in plan_models
    assert "def _battery_command(" not in coordinator
    assert "plan_compiler_adapter" not in production
    assert "self._actuator.apply(command)" in coordinator


def test_application_boundaries_do_not_cross_layer_ownership():
    planning = (PACKAGE / "planning_service.py").read_text(encoding="utf-8")
    market = (PACKAGE / "market_context.py").read_text(encoding="utf-8")
    savings = (PACKAGE / "savings.py").read_text(encoding="utf-8")

    assert all(
        token not in planning
        for token in ("homeassistant", "urlopen", "build_production_forecast")
    )
    assert all(
        token not in market
        for token in ("DynamicProgrammingOptimizer", "ForecastBundle", "BatteryPlan")
    )
    assert all(
        token not in savings
        for token in ("DynamicProgrammingOptimizer", "ForecastBundle", "BatteryPlan")
    )


def test_optimizer_problem_builder_has_no_home_assistant_or_io_dependency():
    source = (PACKAGE / "optimization_problem.py").read_text(encoding="utf-8")
    forbidden = ("homeassistant", "open(", "urlopen", "requests", "sqlalchemy")
    assert all(token not in source for token in forbidden)

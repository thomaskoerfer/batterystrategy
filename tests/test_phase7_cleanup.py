"""Phase-7 regression guards for removed transitional implementations."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.battery_strategy.planning_runtime import PlanningRuntime

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
    first = PlanningRuntime.from_mapping(
        {
            "timezone": "Europe/Berlin",
            "battery_capacity_kwh": 6.0,
            "states": {"battery_soc": 42.0},
        }
    )
    second = PlanningRuntime.from_mapping(
        {
            "timezone": "UTC",
            "battery_capacity_kwh": 10.0,
            "states": {"battery_soc": 81.0},
        }
    )

    assert first.settings.battery_capacity_kwh == 6.0
    assert second.settings.battery_capacity_kwh == 10.0
    assert first.states["battery_soc"] == 42.0
    assert second.states["battery_soc"] == 81.0
    with pytest.raises(TypeError):
        first.states["battery_soc"] = 50.0


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

"""Phase-7 regression guards for removed transitional implementations."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "battery_strategy"


def test_completed_shadow_and_legacy_optimizer_modules_are_absent():
    obsolete = (
        "optimizer_shadow.py",
        "forecast_shadow_runner.py",
        "forecast_shadow_store.py",
        "forecasting/shadow.py",
    )
    assert all(not (PACKAGE / name).exists() for name in obsolete)


def test_optimizer_orchestration_has_no_recorder_schema_dependency():
    source = (PACKAGE / "optimizer_engine.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "sqlalchemy",
        "states_meta",
        "select s.state",
        "db_engine",
        "build_virtual_plan",
        "optimizer_shadow",
    )
    assert all(token not in source for token in forbidden)


def test_runtime_facade_delegates_coarse_application_boundaries():
    source = (PACKAGE / "optimizer_engine.py").read_text(encoding="utf-8")
    forbidden_definitions = (
        "def adapt_pure_optimizer_plan(",
        "def eex_filter_rows(",
        "def eex_fetch_settlement(",
        "def build_eex_proxy_day_prices(",
        "def indexed_value_at_or_before(",
    )
    assert all(token not in source for token in forbidden_definitions)
    assert "PlanningService(" in source
    assert "MarketContextService(" in source
    assert "SavingsLedger(" in source


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

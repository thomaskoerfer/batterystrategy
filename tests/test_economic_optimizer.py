"""Contract and economic regression tests for the pure optimizer."""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

from custom_components.battery_strategy import planning_pipeline
from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    BatteryState,
    CommercialPolicy,
    ForecastBundle,
    ForecastSlot,
    LoadForecast,
    MarketSlot,
    OptimizationProblem,
    PlanMode,
    PvForecast,
    QuantileEnergy,
    SlotKey,
)
from custom_components.battery_strategy.economic_optimizer import (
    DynamicProgrammingOptimizer,
)
from custom_components.battery_strategy.runtime_market_data import TariffInterval
from tests.planning_runtime_helpers import settings_from_values

SLOT_MS = 15 * 60 * 1000
SLOT_H = 0.25


def problem(
    prices,
    *,
    loads=None,
    pv=None,
    soc=10.0,
    terminal=0.0,
    floor=None,
    grid=True,
    pv_charge=True,
    discharge=True,
    rte=0.8,
    start_ms=0,
):
    loads = list(loads or [0.0] * len(prices))
    pv = list(pv or [0.0] * len(prices))
    slots = tuple(
        SlotKey(start_ms + index * SLOT_MS, start_ms + (index + 1) * SLOT_MS)
        for index in range(len(prices))
    )
    load_slots = tuple(
        ForecastSlot(slot, QuantileEnergy(load)) for slot, load in zip(slots, loads)
    )
    pv_slots = tuple(
        ForecastSlot(slot, QuantileEnergy(generation))
        for slot, generation in zip(slots, pv)
    )
    forecast = ForecastBundle(
        LoadForecast("load", start_ms, start_ms, "load-v1", load_slots),
        PvForecast("pv", start_ms, start_ms, "pv-v1", pv_slots),
    )
    return OptimizationProblem(
        problem_id="test-problem",
        as_of_ms=start_ms,
        forecast=forecast,
        market=tuple(MarketSlot(slot, price) for slot, price in zip(slots, prices)),
        battery=BatteryState(start_ms, soc),
        constraints=BatteryConstraints(6.0, 10.0, 100.0, 2400.0, 2400.0, rte),
        policy=CommercialPolicy(
            min_margin_ct_per_kwh=2.0,
            terminal_value_ct_per_kwh=terminal,
            discharge_floor_ct_per_kwh=floor,
            grid_charging_allowed=grid,
            pv_charging_allowed=pv_charge,
            discharge_allowed=discharge,
        ),
    )


def test_optimizer_module_has_no_ha_runtime_or_io_dependencies():
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "battery_strategy"
        / "economic_optimizer.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed = {"__future__", "dataclasses", "math"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {alias.name.split(".", 1)[0] for alias in node.names} <= allowed
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".", 1)[0] in allowed


def test_optimizer_is_deterministic_and_preserves_problem_identity():
    candidate = problem([10.0, 20.0, 40.0], loads=[0.0, 0.1, 0.5])
    optimizer = DynamicProgrammingOptimizer()
    first = optimizer.optimize(candidate)
    second = optimizer.optimize(candidate)
    assert first == second
    assert first.problem_id == candidate.problem_id
    assert first.generated_at_ms == candidate.as_of_ms


def test_profitable_grid_charge_is_used_for_later_expensive_load():
    plan = DynamicProgrammingOptimizer().optimize(
        problem([10.0, 40.0], loads=[0.0, 0.5])
    )
    assert plan.slots[0].mode == PlanMode.CHARGE
    assert plan.slots[0].planned_grid_charge_kwh > 0.0
    assert plan.slots[1].mode == PlanMode.DISCHARGE
    assert plan.slots[1].planned_discharge_kwh > 0.0
    assert plan.optimized_cost_eur < plan.baseline_cost_eur


def test_cheaper_slot_after_peak_cannot_replace_energy_needed_before_it():
    plan = DynamicProgrammingOptimizer().optimize(
        problem(
            [20.0, 40.0, 15.0],
            loads=[0.0, 0.4, 0.0],
            soc=10.0,
            rte=0.8,
        )
    )

    assert plan.slots[0].planned_grid_charge_kwh > 0.0
    assert plan.slots[1].planned_discharge_kwh > 0.0
    assert plan.slots[2].planned_grid_charge_kwh == 0.0
    assert plan.optimized_cost_eur < plan.baseline_cost_eur


def test_cheaper_slot_before_peak_defers_grid_charge():
    plan = DynamicProgrammingOptimizer().optimize(
        problem(
            [20.0, 15.0, 40.0],
            loads=[0.0, 0.0, 0.4],
            soc=10.0,
            rte=0.8,
        )
    )

    assert plan.slots[0].planned_grid_charge_kwh == 0.0
    assert plan.slots[1].planned_grid_charge_kwh > 0.0
    assert plan.slots[2].planned_discharge_kwh > 0.0


def test_insufficient_cheaper_capacity_before_peak_keeps_earlier_charge():
    plan = DynamicProgrammingOptimizer().optimize(
        problem(
            [20.0, 15.0, 40.0, 40.0],
            loads=[0.0, 0.0, 0.6, 0.6],
            soc=10.0,
            rte=0.8,
        )
    )

    assert plan.slots[0].planned_grid_charge_kwh > 0.0
    assert plan.slots[1].planned_grid_charge_kwh > 0.0
    assert sum(slot.planned_discharge_kwh for slot in plan.slots[2:]) > 0.6


def test_round_trip_loss_blocks_uneconomic_cycle():
    plan = DynamicProgrammingOptimizer().optimize(
        problem([30.0, 35.0], loads=[0.0, 0.5], rte=0.8)
    )
    assert all(slot.planned_charge_kwh == 0.0 for slot in plan.slots)
    assert all(slot.planned_discharge_kwh == 0.0 for slot in plan.slots)


def test_grid_charge_permission_does_not_disable_free_pv_charge():
    plan = DynamicProgrammingOptimizer().optimize(
        problem(
            [0.0, 40.0],
            loads=[0.0, 0.5],
            pv=[0.5, 0.0],
            grid=False,
        )
    )
    assert plan.slots[0].planned_pv_charge_kwh > 0.0
    assert plan.slots[0].planned_grid_charge_kwh == 0.0
    assert plan.slots[1].planned_discharge_kwh > 0.0


def test_discharge_floor_is_a_feasibility_guard():
    plan = DynamicProgrammingOptimizer().optimize(
        problem([30.0], loads=[0.5], soc=100.0, floor=35.0)
    )
    assert plan.slots[0].planned_discharge_kwh == 0.0
    assert plan.slots[0].discharge_budget_kwh == 0.0


def test_terminal_value_preserves_inventory_at_horizon():
    without_terminal = DynamicProgrammingOptimizer().optimize(
        problem([40.0], loads=[0.5], soc=50.0)
    )
    with_terminal = DynamicProgrammingOptimizer().optimize(
        problem([40.0], loads=[0.5], soc=50.0, terminal=50.0)
    )
    assert without_terminal.slots[0].planned_discharge_kwh > 0.0
    assert with_terminal.slots[0].planned_discharge_kwh == 0.0


def test_pv_recovery_budget_requires_a_real_headroom_shortage():
    optimizer = DynamicProgrammingOptimizer()
    high_soc = optimizer.optimize(
        problem(
            [35.0, 10.0, 10.0],
            loads=[0.2, 0.0, 0.0],
            pv=[0.0, 1.2, 1.2],
            soc=100.0,
            floor=40.0,
        )
    )
    low_soc = optimizer.optimize(
        problem(
            [35.0, 10.0, 10.0],
            loads=[0.2, 0.0, 0.0],
            pv=[0.0, 1.2, 1.2],
            soc=20.0,
            floor=40.0,
        )
    )
    assert high_soc.slots[0].discharge_budget_kwh > 0.0
    assert low_soc.slots[0].discharge_budget_kwh == pytest.approx(
        low_soc.slots[0].planned_discharge_kwh
    )


def test_disabled_discharge_never_creates_plan_or_budget():
    plan = DynamicProgrammingOptimizer().optimize(
        problem([50.0, 50.0], loads=[0.5, 0.5], soc=100.0, discharge=False)
    )
    assert all(slot.planned_discharge_kwh == 0.0 for slot in plan.slots)
    assert all(slot.discharge_budget_kwh == 0.0 for slot in plan.slots)


@pytest.mark.parametrize(
    ("prices", "loads", "pv", "soc"),
    [
        ([10.0, 10.0, 40.0, 40.0], [0.1, 0.1, 0.5, 0.5], [0.0] * 4, 10.0),
        ([30.0, 15.0, 15.0, 45.0], [0.3] * 4, [0.0, 0.8, 0.8, 0.0], 60.0),
        ([35.0, 35.0, 35.0, 35.0], [0.4] * 4, [0.0] * 4, 80.0),
    ],
)
def test_pure_optimizer_matches_current_economic_kernel(prices, loads, pv, soc):
    start = dt.datetime(2026, 8, 30, tzinfo=dt.UTC)
    intervals = [
        {
            "dt": start + dt.timedelta(minutes=15 * index),
            "price_eur": price / 100.0,
        }
        for index, price in enumerate(prices)
    ]
    candidate = problem(
        prices,
        loads=loads,
        pv=pv,
        soc=soc,
        start_ms=int(start.timestamp() * 1000),
    )
    settings = settings_from_values(
        captured_at_ms=1_800_000_000_000,
        battery_capacity_kwh=6.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_power_w=2400.0,
        max_discharge_power_w=2400.0,
        round_trip_efficiency=0.8,
        min_margin_ct_per_kwh=2.0,
        pv_charging="on",
        grid_charging="price_sensitive",
        discharge="price_sensitive",
        feed_in_tariff_ct_per_kwh=0.0,
    )
    current = (
        planning_pipeline._planning_service(settings)
        .plan(
            intervals=[
                TariffInterval(item["dt"], item["price_eur"]) for item in intervals
            ],
            samples=[],
            start_energy_kwh=6.0 * soc / 100.0,
            forecast_bundle=candidate.forecast,
        )
        .data
    )
    pure = DynamicProgrammingOptimizer().optimize(candidate)
    for point, slot in zip(current["points"], pure.slots, strict=True):
        assert slot.planned_charge_kwh == pytest.approx(
            point["charge_fc_w"] * SLOT_H / 1000.0, abs=2e-5
        )
        assert slot.planned_discharge_kwh == pytest.approx(
            point["discharge_fc_w"] * SLOT_H / 1000.0, abs=2e-5
        )
        assert slot.discharge_budget_kwh == pytest.approx(
            point["discharge_budget_kwh"], abs=5e-4
        )
        assert slot.expected_soc_start_pct == pytest.approx(point["soc_pct"], abs=0.01)
    assert pure.baseline_cost_eur == pytest.approx(
        sum(item["base_eur"] for item in current["daily_costs"].values()),
        abs=5e-4,
    )
    assert pure.optimized_cost_eur == pytest.approx(
        sum(item["with_bat_eur"] for item in current["daily_costs"].values()),
        abs=5e-4,
    )

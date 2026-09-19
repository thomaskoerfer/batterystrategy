"""Contract and economic regression tests for the pure optimizer."""

from __future__ import annotations

import ast
import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from custom_components.battery_strategy import planning_pipeline
from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    BatteryState,
    CommercialPolicy,
    EvInteractionPolicy,
    ForecastBundle,
    ForecastScenario,
    ForecastScenarioSet,
    ForecastScenarioSlot,
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
    StochasticDynamicProgrammingOptimizer,
    _energy_lattice,
    _scenario_flows,
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
    allowed = {"__future__", "bisect", "dataclasses", "math"}
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


def test_stochastic_optimizer_uses_coherent_ev_paths_and_common_first_action():
    candidate = problem([10.0, 50.0], loads=[0.0, 0.0], soc=10.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ForecastScenarioSet(
        "scenarios",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        (
            ForecastScenario(
                "ev-later",
                0.5,
                (
                    ForecastScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ForecastScenarioSlot(slots[1], 0.0, 0.0, 0.5),
                ),
            ),
            ForecastScenario(
                "house-later",
                0.5,
                (
                    ForecastScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ForecastScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
        ),
    )
    candidate = replace(
        candidate,
        forecast=replace(candidate.forecast, scenarios=scenarios),
        ev_policy=EvInteractionPolicy(battery_may_feed_ev=True),
    )

    p50 = DynamicProgrammingOptimizer().optimize(candidate)
    stochastic = StochasticDynamicProgrammingOptimizer().optimize(candidate)

    assert p50.slots[0].planned_charge_kwh == 0.0
    assert stochastic.slots[0].planned_grid_charge_kwh > 0.0
    assert stochastic.optimizer_version.startswith("stochastic-")


def test_stochastic_optimizer_falls_back_exactly_without_scenarios():
    candidate = problem([10.0, 50.0], loads=[0.0, 0.5], soc=10.0)
    assert StochasticDynamicProgrammingOptimizer().optimize(candidate) == (
        DynamicProgrammingOptimizer().optimize(candidate)
    )


def test_stochastic_first_budget_cannot_exceed_common_discharge_action():
    candidate = problem([50.0, 10.0], loads=[0.4, 0.0], soc=80.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ForecastScenarioSet(
        "paths",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        tuple(
            ForecastScenario(
                str(index),
                0.5,
                (
                    ForecastScenarioSlot(slots[0], load, 0.0, 0.0),
                    ForecastScenarioSlot(slots[1], 0.0, 0.0, 0.0),
                ),
            )
            for index, load in enumerate((0.2, 0.5))
        ),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(
        replace(candidate, forecast=replace(candidate.forecast, scenarios=scenarios))
    )

    assert plan.slots[0].discharge_budget_kwh == pytest.approx(
        plan.slots[0].planned_discharge_kwh
    )


def test_stochastic_first_action_respects_sub_lattice_power_limit():
    candidate = problem([10.0, 50.0], loads=[0.0, 0.0], soc=10.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ForecastScenarioSet(
        "paths",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        (
            ForecastScenario(
                "future-load",
                1.0,
                (
                    ForecastScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ForecastScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
        ),
    )
    candidate = replace(
        candidate,
        forecast=replace(candidate.forecast, scenarios=scenarios),
        constraints=replace(candidate.constraints, max_charge_power_w=180.0),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(candidate)

    assert 0.0 < plan.slots[0].planned_charge_kwh <= 0.045 + 1e-9


def test_stochastic_common_discharge_is_not_rejected_by_zero_p50_load():
    candidate = problem([50.0, 10.0], loads=[0.0, 0.0], soc=50.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ForecastScenarioSet(
        "paths",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        tuple(
            ForecastScenario(
                f"load-{load}",
                0.5,
                (
                    ForecastScenarioSlot(slots[0], load, 0.0, 0.0),
                    ForecastScenarioSlot(slots[1], 0.0, 0.0, 0.0),
                ),
            )
            for load in (0.4, 0.5)
        ),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(
        replace(candidate, forecast=replace(candidate.forecast, scenarios=scenarios))
    )

    assert plan.slots[0].planned_discharge_kwh > 0.0
    assert plan.slots[0].discharge_budget_kwh == pytest.approx(
        plan.slots[0].planned_discharge_kwh
    )


def test_stochastic_energy_lattice_never_crosses_physical_endpoint():
    lattice = _energy_lattice(0.0, 0.55, 0.1)

    assert lattice[-1] == pytest.approx(0.55)
    assert all(0.0 <= item <= 0.55 for item in lattice)
    assert lattice == tuple(sorted(set(lattice)))


def test_required_first_transition_is_not_shifted_by_source_canonicalization():
    candidate = problem(
        [10.0, 5.0, 50.0],
        loads=[0.0, 0.0, 0.5],
        pv=[0.25, 0.0, 0.0],
        soc=10.0,
    )
    required_end = 0.825
    optimizer = DynamicProgrammingOptimizer()
    plan = optimizer._build_plan(
        candidate,
        (10.0, 5.0, 50.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.5),
        (0.25, 0.0, 0.0),
        (0.0, 0.0, 0.5),
        required_first_end_kwh=required_end,
    )

    assert plan.slots[0].expected_soc_end_pct == pytest.approx(
        100.0 * required_end / candidate.constraints.capacity_kwh
    )
    assert plan.slots[0].planned_grid_charge_kwh > 0.0


def test_scenario_ev_discharge_limit_matches_live_no_ev_residual():
    slot = SlotKey(0, 900_000)
    demand, charge_surplus, physical_surplus, limit = _scenario_flows(
        (ForecastScenarioSlot(slot, 0.5, 0.8, 0.8),),
        EvInteractionPolicy(
            pv_to_ev_first=True,
            discharge_during_ev_charging=True,
            battery_may_feed_ev=False,
        ),
    )

    assert demand == pytest.approx((0.5,))
    assert charge_surplus == pytest.approx((0.0,))
    assert physical_surplus == pytest.approx((0.0,))
    assert limit == pytest.approx((0.0,))


def test_scenario_battery_priority_prices_pv_diverted_from_ev_as_grid_energy():
    slot = SlotKey(0, 900_000)
    demand, charge_surplus, physical_surplus, limit = _scenario_flows(
        (ForecastScenarioSlot(slot, 0.5, 0.8, 0.8),),
        EvInteractionPolicy(
            pv_to_ev_first=False,
            discharge_during_ev_charging=True,
            battery_may_feed_ev=False,
            ev_active_threshold_w=300.0,
        ),
    )

    assert demand == pytest.approx((0.5,))
    assert charge_surplus == pytest.approx((0.3,))
    assert physical_surplus == pytest.approx((0.0,))
    assert limit == pytest.approx((0.0,))


def test_scenario_ev_below_threshold_is_not_treated_as_active():
    slot = SlotKey(0, 900_000)
    _demand, _charge_surplus, _physical_surplus, limit = _scenario_flows(
        (ForecastScenarioSlot(slot, 0.5, 0.0, 0.05),),
        EvInteractionPolicy(
            discharge_during_ev_charging=False,
            battery_may_feed_ev=False,
            ev_active_threshold_w=300.0,
        ),
    )

    assert limit == pytest.approx((0.55,))


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


def test_cutover_uses_stochastic_plan_when_scenarios_are_available():
    start = dt.datetime(2026, 9, 19, tzinfo=dt.UTC)
    base = problem(
        [10.0, 50.0],
        loads=[0.0, 0.0],
        soc=10.0,
        start_ms=int(start.timestamp() * 1000),
    )
    slots = tuple(item.slot for item in base.forecast.load.slots)
    scenarios = ForecastScenarioSet(
        "shadow",
        base.as_of_ms,
        base.as_of_ms,
        "weekly-v1",
        (
            ForecastScenario(
                "path",
                1.0,
                (
                    ForecastScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ForecastScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
        ),
    )
    settings = settings_from_values(
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
    )
    intervals = [
        TariffInterval(start + dt.timedelta(minutes=15 * index), price / 100.0)
        for index, price in enumerate((10.0, 50.0))
    ]
    service = planning_pipeline._planning_service(settings)

    without = service.plan(
        intervals=intervals,
        samples=[],
        start_energy_kwh=0.6,
        forecast_bundle=base.forecast,
    )
    with_shadow = service.plan(
        intervals=intervals,
        samples=[],
        start_energy_kwh=0.6,
        forecast_bundle=replace(base.forecast, scenarios=scenarios),
    )

    assert with_shadow.battery_plan != without.battery_plan
    assert with_shadow.battery_plan.optimizer_version == "stochastic-two-stage-dp-v1"


def test_cutover_complexity_guard_falls_back_to_p50():
    start = dt.datetime(2026, 9, 19, tzinfo=dt.UTC)
    base = problem(
        [10.0, 50.0],
        loads=[0.0, 0.0],
        soc=10.0,
        start_ms=int(start.timestamp() * 1000),
    )
    slots = tuple(item.slot for item in base.forecast.load.slots)
    scenarios = ForecastScenarioSet(
        "paths",
        base.as_of_ms,
        base.as_of_ms,
        "weekly-v1",
        (
            ForecastScenario(
                "path",
                1.0,
                tuple(ForecastScenarioSlot(slot, 0.2, 0.0, 0.0) for slot in slots),
            ),
        ),
    )
    settings = settings_from_values(
        battery_capacity_kwh=100.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_power_w=2400.0,
        max_discharge_power_w=2400.0,
        round_trip_efficiency=0.8,
        min_margin_ct_per_kwh=2.0,
        pv_charging="on",
        grid_charging="price_sensitive",
        discharge="price_sensitive",
    )
    intervals = [
        TariffInterval(start + dt.timedelta(minutes=15 * index), price / 100.0)
        for index, price in enumerate((10.0, 50.0))
    ]

    result = planning_pipeline._planning_service(settings).plan(
        intervals=intervals,
        samples=[],
        start_energy_kwh=10.0,
        forecast_bundle=replace(base.forecast, scenarios=scenarios),
    )

    assert result.battery_plan.optimizer_version.endswith(
        "-stochastic-complexity-fallback"
    )
    assert (
        result.data["forecast_diagnostics"]["optimizer"]["fallback_reason"]
        == "stochastic_complexity_guard"
    )


def test_cutover_mid_slot_guard_keeps_full_slot_stochastic_action_non_authoritative():
    start = dt.datetime(2026, 9, 19, tzinfo=dt.UTC)
    base = problem(
        [10.0, 50.0],
        loads=[0.0, 0.0],
        soc=10.0,
        start_ms=int(start.timestamp() * 1000),
    )
    late_ms = base.as_of_ms + 120_000
    slots = tuple(item.slot for item in base.forecast.load.slots)
    scenarios = ForecastScenarioSet(
        "late-paths",
        late_ms,
        late_ms,
        "weekly-v1",
        (
            ForecastScenario(
                "path",
                1.0,
                tuple(ForecastScenarioSlot(slot, 0.4, 0.0, 0.0) for slot in slots),
            ),
        ),
    )
    late_bundle = replace(
        base.forecast,
        load=replace(base.forecast.load, generated_at_ms=late_ms),
        pv=replace(base.forecast.pv, generated_at_ms=late_ms),
        scenarios=scenarios,
    )
    settings = settings_from_values(
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
    )
    intervals = [
        TariffInterval(start + dt.timedelta(minutes=15 * index), price / 100.0)
        for index, price in enumerate((10.0, 50.0))
    ]

    result = planning_pipeline._planning_service(settings).plan(
        intervals=intervals,
        samples=[],
        start_energy_kwh=0.6,
        forecast_bundle=late_bundle,
    )

    assert result.battery_plan.optimizer_version.endswith(
        "-stochastic-mid-slot-fallback"
    )
    assert (
        result.data["forecast_diagnostics"]["optimizer"]["fallback_reason"]
        == "stochastic_mid_slot_guard"
    )

"""Contract and economic regression tests for the pure optimizer."""

from __future__ import annotations

import ast
import datetime as dt
import math
from dataclasses import replace
from pathlib import Path

import pytest

from custom_components.battery_strategy import planning_pipeline
from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    BatteryState,
    CommercialPolicy,
    EvForecast,
    EvForecastSlot,
    EvInteractionPolicy,
    ForecastDistributionBundle,
    ForecastSlot,
    LoadForecast,
    MarketSlot,
    OptimizationProblem,
    PlanMode,
    PvForecast,
    QuantileEnergy,
    ScenarioBuildDiagnostics,
    ScenarioBuildResult,
    ScenarioBuildStatus,
    ScenarioBundle,
    ScenarioPath,
    ScenarioSlot,
    SlotKey,
)
from custom_components.battery_strategy.economic_optimizer import (
    DynamicProgrammingOptimizer,
    StochasticDynamicProgrammingOptimizer,
    _Action,
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
    forecast = ForecastDistributionBundle(
        LoadForecast("load", start_ms, start_ms, "load-v1", load_slots),
        PvForecast("pv", start_ms, start_ms, "pv-v1", pv_slots),
        EvForecast(
            "ev",
            start_ms,
            start_ms,
            "ev-v1",
            tuple(
                EvForecastSlot(slot, QuantileEnergy(0.0), 0.0, 0.0) for slot in slots
            ),
        ),
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
    scenarios = ScenarioBundle(
        "scenarios",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        (
            ScenarioPath(
                "ev-later",
                0.5,
                (
                    ScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ScenarioSlot(slots[1], 0.0, 0.0, 0.5),
                ),
            ),
            ScenarioPath(
                "house-later",
                0.5,
                (
                    ScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
        ),
    )
    candidate = replace(
        candidate,
        scenarios=scenarios,
        ev_policy=EvInteractionPolicy(battery_may_feed_ev=True),
    )

    p50 = DynamicProgrammingOptimizer().optimize(candidate)
    stochastic = StochasticDynamicProgrammingOptimizer().optimize(candidate)

    assert p50.slots[0].planned_charge_kwh == 0.0
    assert stochastic.slots[0].planned_grid_charge_kwh > 0.0
    assert stochastic.optimizer_version.startswith("stochastic-")


def test_stochastic_required_charge_remains_executable_when_pv_is_uncertain():
    candidate = problem([10.0, 50.0], loads=[0.0, 0.0], soc=10.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ScenarioBundle(
        "mixed-pv",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        (
            ScenarioPath(
                "sun",
                0.5,
                (
                    ScenarioSlot(slots[0], 0.0, 0.6, 0.0),
                    ScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
            ScenarioPath(
                "cloud",
                0.5,
                (
                    ScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
        ),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(
        replace(candidate, scenarios=scenarios)
    )

    assert plan.slots[0].required_charge_kwh > 0.0
    assert plan.slots[0].planned_grid_charge_kwh > 0.0


def test_stochastic_first_charge_respects_actual_battery_headroom():
    candidate = problem([1.0, 60.0], loads=[0.0, 0.6], soc=99.5)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ScenarioBundle(
        "headroom",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "test-v1",
        (
            ScenarioPath(
                "path",
                1.0,
                tuple(
                    ScenarioSlot(slot, load, 0.0, 0.0)
                    for slot, load in zip(slots, (0.0, 0.6), strict=True)
                ),
            ),
        ),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(
        replace(candidate, scenarios=scenarios)
    )

    eta = math.sqrt(candidate.constraints.round_trip_efficiency)
    headroom_input = (
        candidate.constraints.capacity_kwh
        * (candidate.constraints.max_soc_pct - candidate.battery.soc_pct)
        / 100.0
        / eta
    )
    assert plan.slots[0].required_charge_kwh <= headroom_input + 1e-9


def test_stochastic_optimizer_falls_back_exactly_without_scenarios():
    candidate = problem([10.0, 50.0], loads=[0.0, 0.5], soc=10.0)
    assert StochasticDynamicProgrammingOptimizer().optimize(candidate) == (
        DynamicProgrammingOptimizer().optimize(candidate)
    )


def test_stochastic_first_budget_is_common_permission_not_p50_target():
    candidate = problem([50.0, 10.0], loads=[0.4, 0.0], soc=80.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ScenarioBundle(
        "paths",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        tuple(
            ScenarioPath(
                str(index),
                0.5,
                (
                    ScenarioSlot(slots[0], load, 0.0, 0.0),
                    ScenarioSlot(slots[1], 0.0, 0.0, 0.0),
                ),
            )
            for index, load in enumerate((0.2, 0.5))
        ),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(
        replace(candidate, scenarios=scenarios)
    )

    assert plan.slots[0].planned_discharge_kwh <= plan.slots[0].discharge_budget_kwh
    assert plan.slots[0].discharge_budget_kwh <= 0.6


def test_stochastic_policy_replay_accepts_continuous_executable_budget():
    candidate = problem([50.0, 10.0], loads=[0.4, 0.0], soc=80.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ScenarioBundle(
        "paths",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        (
            ScenarioPath(
                "path",
                1.0,
                (
                    ScenarioSlot(slots[0], 0.4, 0.0, 0.0),
                    ScenarioSlot(slots[1], 0.0, 0.0, 0.0),
                ),
            ),
        ),
    )
    candidate = replace(candidate, scenarios=scenarios)

    plan, diagnostics = (
        StochasticDynamicProgrammingOptimizer().optimize_with_diagnostics(
            candidate, required_first_policy=(0.0, 0.12345)
        )
    )

    assert plan.slots[0].discharge_budget_kwh == pytest.approx(0.12345)
    assert math.isfinite(diagnostics["expected_scenario_cost_eur"])


def test_stochastic_first_action_respects_sub_lattice_power_limit():
    candidate = problem([10.0, 50.0], loads=[0.0, 0.0], soc=10.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ScenarioBundle(
        "paths",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        (
            ScenarioPath(
                "future-load",
                1.0,
                (
                    ScenarioSlot(slots[0], 0.0, 0.0, 0.0),
                    ScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
        ),
    )
    candidate = replace(
        candidate,
        scenarios=scenarios,
        constraints=replace(candidate.constraints, max_charge_power_w=180.0),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(candidate)

    assert 0.0 < plan.slots[0].planned_charge_kwh <= 0.045 + 1e-9


def test_stochastic_common_budget_survives_zero_p50_load():
    candidate = problem([50.0, 10.0], loads=[0.0, 0.0], soc=50.0)
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ScenarioBundle(
        "paths",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        tuple(
            ScenarioPath(
                f"load-{load}",
                0.5,
                (
                    ScenarioSlot(slots[0], load, 0.0, 0.0),
                    ScenarioSlot(slots[1], 0.0, 0.0, 0.0),
                ),
            )
            for load in (0.4, 0.5)
        ),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(
        replace(candidate, scenarios=scenarios)
    )

    assert plan.slots[0].planned_discharge_kwh == 0.0
    assert plan.slots[0].discharge_budget_kwh > 0.0


def _with_probability_one_scenario(candidate, loads, pv=None):
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    pv = tuple(pv or (0.0,) * len(slots))
    return replace(
        candidate,
        scenarios=ScenarioBundle(
            "p50-explicit",
            "test",
            candidate.as_of_ms,
            candidate.as_of_ms,
            "test-v1",
            (
                ScenarioPath(
                    "only",
                    1.0,
                    tuple(
                        ScenarioSlot(slot, load, generation, 0.0)
                        for slot, load, generation in zip(slots, loads, pv, strict=True)
                    ),
                ),
            ),
        ),
    )


@pytest.mark.parametrize("current_load", (0.1, 0.5))
def test_commercial_budget_is_not_capped_by_expected_current_load(current_load):
    loads = (current_load, 0.05, 0.0, 0.0, 0.6, 0.6)
    candidate = problem(
        [41.34, 48.79, 15.0, 15.0, 45.0, 45.0],
        loads=loads,
        soc=23.0,
        rte=0.8,
    )

    deterministic = DynamicProgrammingOptimizer().optimize(candidate)
    stochastic = StochasticDynamicProgrammingOptimizer().optimize(
        _with_probability_one_scenario(candidate, loads)
    )

    assert deterministic.slots[0].discharge_budget_kwh == pytest.approx(0.6)
    assert stochastic.slots[0].discharge_budget_kwh == pytest.approx(0.6)
    assert deterministic.slots[0].planned_discharge_kwh <= current_load
    assert stochastic.slots[0].planned_discharge_kwh == pytest.approx(current_load)


def test_forecast_pv_charge_keeps_budget_for_unexpected_house_load():
    loads = (0.0, 0.6)
    pv = (0.5, 0.0)
    candidate = problem(
        [50.0, 40.0],
        loads=loads,
        pv=pv,
        soc=20.0,
        rte=0.8,
    )

    deterministic = DynamicProgrammingOptimizer().optimize(candidate)
    stochastic = StochasticDynamicProgrammingOptimizer().optimize(
        _with_probability_one_scenario(candidate, loads, pv)
    )

    assert deterministic.slots[0].planned_pv_charge_kwh > 0.0
    assert deterministic.slots[0].planned_grid_charge_kwh == 0.0
    assert deterministic.slots[0].discharge_budget_kwh > 0.0
    assert deterministic.slots[0].discharge_budget_kwh == pytest.approx(
        stochastic.slots[0].discharge_budget_kwh
    )


def test_tiny_grid_charge_residue_excludes_discharge_budget():
    candidate = problem([50.0], loads=[0.0], pv=[0.5], soc=20.0, rte=0.8)
    action = _Action(
        charge_kwh=0.5,
        pv_charge_kwh=0.5 - 5e-7,
        grid_charge_kwh=5e-7,
        soc_start_kwh=1.2,
    )

    budgets = DynamicProgrammingOptimizer()._discharge_budgets(
        candidate,
        (50.0,),
        (0.0,),
        (0.0,),
        (0.5,),
        (action,),
    )

    assert budgets == [0.0]


def test_commercial_budget_reserves_inventory_without_economic_recharge():
    loads = (0.1, 0.6)
    candidate = problem([41.34, 70.0], loads=loads, soc=23.0, rte=0.8)

    deterministic = DynamicProgrammingOptimizer().optimize(candidate)
    stochastic = StochasticDynamicProgrammingOptimizer().optimize(
        _with_probability_one_scenario(candidate, loads)
    )

    assert deterministic.slots[0].discharge_budget_kwh < 0.6
    assert stochastic.slots[0].discharge_budget_kwh < 0.6


def test_commercial_budget_releases_only_rechargeable_inventory():
    no_loads = (0.1, 0.6)
    recharge_loads = (0.1, 0.0, 0.6)
    without_recharge = problem([41.34, 70.0], loads=no_loads, soc=23.0, rte=0.8)
    with_recharge = problem(
        [41.34, 15.0, 70.0], loads=recharge_loads, soc=23.0, rte=0.8
    )
    partial_recharge = replace(
        with_recharge,
        constraints=replace(with_recharge.constraints, max_charge_power_w=800.0),
    )

    cases = (
        (
            DynamicProgrammingOptimizer(),
            without_recharge,
            partial_recharge,
            with_recharge,
        ),
        (
            StochasticDynamicProgrammingOptimizer(),
            _with_probability_one_scenario(without_recharge, no_loads),
            _with_probability_one_scenario(partial_recharge, recharge_loads),
            _with_probability_one_scenario(with_recharge, recharge_loads),
        ),
    )
    for optimizer, no_recharge, partial, complete in cases:
        no_budget = optimizer.optimize(no_recharge).slots[0].discharge_budget_kwh
        partial_budget = optimizer.optimize(partial).slots[0].discharge_budget_kwh
        complete_budget = optimizer.optimize(complete).slots[0].discharge_budget_kwh
        assert no_budget < partial_budget < complete_budget
        assert complete_budget == pytest.approx(0.6)


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
        (ScenarioSlot(slot, 0.5, 0.8, 0.8),),
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
        (ScenarioSlot(slot, 0.5, 0.8, 0.8),),
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


def test_battery_priority_does_not_serialize_diverted_pv_as_grid_charge():
    candidate = problem(
        [10.0, 50.0],
        loads=[0.5, 0.5],
        pv=[0.8, 0.0],
        soc=10.0,
        grid=False,
    )
    slots = tuple(item.slot for item in candidate.forecast.load.slots)
    scenarios = ScenarioBundle(
        "ev-pv",
        "source",
        candidate.as_of_ms,
        candidate.as_of_ms,
        "weekly-v1",
        (
            ScenarioPath(
                "path",
                1.0,
                (
                    ScenarioSlot(slots[0], 0.5, 0.8, 0.8),
                    ScenarioSlot(slots[1], 0.5, 0.0, 0.0),
                ),
            ),
        ),
    )
    candidate = replace(
        candidate,
        scenarios=scenarios,
        ev_policy=EvInteractionPolicy(
            pv_to_ev_first=False,
            battery_may_feed_ev=False,
        ),
    )

    plan = StochasticDynamicProgrammingOptimizer().optimize(candidate)

    assert plan.slots[0].planned_charge_kwh > 0.0
    assert plan.slots[0].planned_grid_charge_kwh == pytest.approx(0.0)
    assert plan.slots[0].planned_pv_charge_kwh == pytest.approx(
        plan.slots[0].planned_charge_kwh
    )


def test_scenario_ev_below_threshold_is_not_treated_as_active():
    slot = SlotKey(0, 900_000)
    _demand, _charge_surplus, _physical_surplus, limit = _scenario_flows(
        (ScenarioSlot(slot, 0.5, 0.0, 0.05),),
        EvInteractionPolicy(
            discharge_during_ev_charging=False,
            battery_may_feed_ev=False,
            ev_active_threshold_w=300.0,
        ),
    )

    assert limit == pytest.approx((0.55,))


def test_planning_service_selects_stochastic_optimizer_for_valid_scenarios():
    start = dt.datetime(2026, 9, 19, tzinfo=dt.UTC)
    base = problem(
        [10.0, 50.0],
        loads=[0.0, 0.0],
        soc=10.0,
        start_ms=int(start.timestamp() * 1000),
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

    scenarios = ScenarioBundle(
        "scenario-set",
        "source",
        base.as_of_ms,
        base.as_of_ms,
        "scenario-v1",
        (
            ScenarioPath(
                "path",
                1.0,
                tuple(
                    ScenarioSlot(slot.slot, 0.4, 0.0, 0.0)
                    for slot in base.forecast.load.slots
                ),
            ),
        ),
    )
    scenario_result = ScenarioBuildResult(
        scenarios,
        ScenarioBuildStatus.COMPLETED,
        ScenarioBuildDiagnostics(
            True,
            1,
            1,
            0,
            0,
            (),
            0.0,
            0,
            (),
            "evidence",
            base.as_of_ms,
            0,
            "input",
            "v1",
        ),
    )
    publication = service.plan(
        intervals=intervals,
        samples=[],
        start_energy_kwh=0.6,
        forecast_bundle=base.forecast,
        scenario_result=scenario_result,
    )

    assert publication.battery_plan.optimizer_version == "stochastic-two-stage-dp-v1"
    selection = publication.data["forecast_diagnostics"]["optimizer_selection"]
    assert selection["status"] == "authoritative_stochastic"
    assert selection["scenario_count"] == 1


def test_planning_service_falls_back_to_deterministic_without_scenarios():
    start = dt.datetime(2026, 9, 19, tzinfo=dt.UTC)
    base = problem([10.0, 50.0], start_ms=int(start.timestamp() * 1000))
    settings = settings_from_values()
    intervals = [
        TariffInterval(start + dt.timedelta(minutes=15 * index), price / 100.0)
        for index, price in enumerate((10.0, 50.0))
    ]

    publication = planning_pipeline._planning_service(settings).plan(
        intervals=intervals,
        samples=[],
        start_energy_kwh=0.6,
        forecast_bundle=base.forecast,
    )

    assert publication.battery_plan.optimizer_version == "economic-dp-v2"
    selection = publication.data["forecast_diagnostics"]["optimizer_selection"]
    assert selection["status"] == "deterministic_fallback"


def test_planning_service_contains_stochastic_failure(monkeypatch):
    start = dt.datetime(2026, 9, 19, tzinfo=dt.UTC)
    base = problem([10.0, 50.0], start_ms=int(start.timestamp() * 1000))
    scenarios = ScenarioBundle(
        "scenario-set",
        "source",
        base.as_of_ms,
        base.as_of_ms,
        "scenario-v1",
        (
            ScenarioPath(
                "path",
                1.0,
                tuple(
                    ScenarioSlot(slot.slot, 0.4, 0.0, 0.0)
                    for slot in base.forecast.load.slots
                ),
            ),
        ),
    )
    result = ScenarioBuildResult(
        scenarios,
        ScenarioBuildStatus.COMPLETED,
        ScenarioBuildDiagnostics(
            True,
            1,
            1,
            0,
            0,
            (),
            0.0,
            0,
            (),
            "evidence",
            base.as_of_ms,
            0,
            "input",
            "v1",
        ),
    )
    monkeypatch.setattr(
        StochasticDynamicProgrammingOptimizer,
        "optimize_with_diagnostics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    intervals = [
        TariffInterval(start + dt.timedelta(minutes=15 * index), price / 100.0)
        for index, price in enumerate((10.0, 50.0))
    ]

    publication = planning_pipeline._planning_service(settings_from_values()).plan(
        intervals=intervals,
        samples=[],
        start_energy_kwh=0.6,
        forecast_bundle=base.forecast,
        scenario_result=result,
    )

    assert publication.battery_plan.optimizer_version == "economic-dp-v2"
    assert publication.optimization_problem.scenarios is None
    selection = publication.data["forecast_diagnostics"]["optimizer_selection"]
    assert selection["fallback_reason"] == "stochastic_optimizer_failed"


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
    assert with_terminal.slots[0].discharge_budget_kwh == 0.0


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

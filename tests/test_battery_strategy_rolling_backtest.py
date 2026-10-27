"""Physical and causal regression checks for workstation-only rolling replay."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.battery_strategy.contracts import (
    CommandMode,
    EvInteractionPolicy,
    LiveControlState,
)
from custom_components.battery_strategy.economic_optimizer import (
    DynamicProgrammingOptimizer,
)
from custom_components.battery_strategy.forecast_trace import _serialize_problem
from scripts import battery_strategy_rolling_backtest as replay
from tests.test_economic_optimizer import problem


def idle_state():
    return LiveControlState(CommandMode.IDLE, 0, None)


def observation(house=0.5, pv=0.0, ev=0.0, start=0):
    return {
        "start_ms": start,
        "house_load_no_ev_kwh": house,
        "pv_generation_kwh": pv,
        "ev_charge_kwh": ev,
        "coverage": 1.0,
        "flags": [],
    }


def plan_with_budget(start=0, budget=0.6):
    candidate = problem([50.0], loads=[0.5], soc=80, start_ms=start)
    plan = DynamicProgrammingOptimizer().optimize(candidate)
    return replace(
        plan,
        slots=(
            replace(
                plan.slots[0],
                discharge_budget_kwh=budget,
                planned_discharge_kwh=min(plan.slots[0].planned_discharge_kwh, budget),
            ),
        ),
    )


def test_dispatch_never_follows_ev_and_preserves_energy():
    plan = plan_with_budget()
    start = plan.constraints.capacity_kwh * 0.8
    flows, _ = replay.dispatch_slot(
        plan, observation(ev=1), start, EvInteractionPolicy(), idle_state()
    )
    assert flows["discharge_kwh"] == pytest.approx(0.5, abs=0.001)
    assert flows["grid_import_kwh"] == pytest.approx(1.0, abs=0.001)
    eta = plan.constraints.round_trip_efficiency**0.5
    assert flows["end_energy_kwh"] == pytest.approx(
        start - flows["discharge_kwh"] / eta
    )


def test_ev_policy_can_block_all_discharge():
    plan = plan_with_budget()
    flows, _ = replay.dispatch_slot(
        plan,
        observation(ev=1),
        4.8,
        EvInteractionPolicy(discharge_during_ev_charging=False),
        idle_state(),
    )
    assert flows["discharge_kwh"] == 0


def test_budget_exhaustion_stops_discharge_without_creating_export():
    plan = plan_with_budget(budget=0.07)
    flows, _ = replay.dispatch_slot(
        plan, observation(), 4.8, EvInteractionPolicy(), idle_state()
    )
    assert flows["discharge_kwh"] == pytest.approx(0.07)
    assert flows["export_kwh"] == 0


@pytest.mark.parametrize("house", [0.0, 0.0001])
def test_zero_or_sub_watt_demand_stays_idle_with_available_discharge_budget(house):
    plan = plan_with_budget()
    flows, _ = replay.dispatch_slot(
        plan, observation(house=house), 4.8, EvInteractionPolicy(), idle_state()
    )
    assert flows["charge_kwh"] == 0
    assert flows["discharge_kwh"] == 0


def test_pv_follow_is_allowed_even_with_zero_budget_and_no_grid_charge():
    plan = plan_with_budget(budget=0)
    plan = replace(plan, slots=(replace(plan.slots[0], grid_charge_allowed=False),))
    flows, _ = replay.dispatch_slot(
        plan,
        observation(house=0.1, pv=0.4, ev=0.1),
        2,
        EvInteractionPolicy(),
        idle_state(),
    )
    assert flows["charge_kwh"] == pytest.approx(0.2, abs=0.001)
    assert flows["grid_import_kwh"] == 0
    assert flows["discharge_kwh"] == 0


def test_dispatch_respects_capacity_and_does_not_reset_between_slots():
    first = plan_with_budget(budget=0.3)
    start = 0.8
    flows, state = replay.dispatch_slot(
        first, observation(), start, EvInteractionPolicy(), idle_state()
    )
    second = plan_with_budget(start=replay.SLOT_MS)
    after, _ = replay.dispatch_slot(
        second,
        observation(start=replay.SLOT_MS),
        flows["end_energy_kwh"],
        EvInteractionPolicy(),
        state,
    )
    minimum = first.constraints.capacity_kwh * first.constraints.min_soc_pct / 100
    assert after["end_energy_kwh"] >= minimum - 1e-9
    assert (
        flows["discharge_kwh"] + after["discharge_kwh"]
        <= (start - minimum) * first.constraints.round_trip_efficiency**0.5 + 1e-9
    )


def trace_at(start):
    p = problem([10.0, 40.0], loads=[0.1, 0.2], start_ms=start)
    p = replace(p, market=tuple(replace(slot, source="tibber") for slot in p.market))
    return {
        "generated_at_ms": start,
        "truncated": False,
        "load": {"slots": [[start, start + replay.SLOT_MS]]},
        "optimization_problem": _serialize_problem(p),
    }


def test_window_does_not_bridge_actual_gaps_or_changed_constraints():
    traces = [trace_at(i * replay.SLOT_MS) for i in range(6)]
    actuals = {
        i * replay.SLOT_MS: observation(start=i * replay.SLOT_MS) for i in range(6)
    }
    actuals[replay.SLOT_MS]["flags"] = ["missing_grid"]
    for trace in traces[4:]:
        trace["optimization_problem"]["constraints"]["min_soc_pct"] = 20
    window, counts = replay.select_window(traces, actuals, 10 * replay.SLOT_MS)
    assert len(window) == 2
    assert window[0]["generated_at_ms"] == 2 * replay.SLOT_MS
    assert counts["invalid_actual"] == 1


def test_inventory_is_credited_identically_not_treated_as_free_savings():
    row = {
        "bill_eur": 1,
        "discharge_kwh": 0,
        "charge_kwh": 0,
        "end_energy_kwh": 2,
        "grid_import_kwh": 3,
        "export_kwh": 0,
        "loss_kwh": 0,
    }
    summary = replay.summarize([row], 1, 30, 1, 0.2)
    assert summary["inventory_adjusted_bill_eur"] == pytest.approx(0.7)
    assert summary["bill_excluding_fixed_ev_baseline_eur"] == pytest.approx(0.8)


def test_invalid_perfect_foresight_bound_is_not_silently_reported():
    with pytest.raises(ValueError, match="bound invalid"):
        replay.verify_benchmark(
            {
                "production": {"economic_objective_eur": 1},
                "perfect_foresight": {"economic_objective_eur": 2},
            },
            {"production": [{"charge_kwh": 0, "discharge_kwh": 0}]},
        )


def test_perfect_foresight_milp_prevents_same_slot_loops_and_respects_ev():
    pytest.importorskip("scipy")
    traces = [trace_at(i * replay.SLOT_MS) for i in range(2)]
    actuals = {
        i * replay.SLOT_MS: observation(house=0, ev=0.6, start=i * replay.SLOT_MS)
        for i in range(2)
    }
    for trace in traces:
        trace["optimization_problem"]["ev_policy"]["discharge_during_ev_charging"] = (
            False
        )
        trace["optimization_problem"]["market"][0][1] = -5
    rows, _ = replay.perfect_foresight(traces, actuals, 1, 0)
    assert all(row["discharge_kwh"] < 1e-9 for row in rows)
    assert all(min(row["grid_import_kwh"], row["export_kwh"]) < 1e-9 for row in rows)


@pytest.mark.parametrize(
    "grid_allowed,pv_allowed,expected_max",
    [
        (True, True, 0.6),
        (False, True, 0.4),
        (True, False, 0),
        (False, False, 0),
    ],
)
def test_perfect_foresight_respects_each_charge_source_permission(
    grid_allowed,
    pv_allowed,
    expected_max,
):
    pytest.importorskip("scipy")
    trace = trace_at(0)
    trace["optimization_problem"]["commercial_policy"].update(
        grid_charging_allowed=grid_allowed,
        pv_charging_allowed=pv_allowed,
    )
    trace["optimization_problem"]["market"][0][1] = -5
    rows, _ = replay.perfect_foresight(
        [trace], {0: observation(house=0, pv=0.4)}, 1, 50
    )
    assert rows[0]["charge_kwh"] == pytest.approx(expected_max)
    assert rows[0]["discharge_kwh"] < 1e-9


def test_perfect_foresight_can_charge_grid_when_pv_charge_is_disabled_without_surplus():
    pytest.importorskip("scipy")
    trace = trace_at(0)
    trace["optimization_problem"]["commercial_policy"]["pv_charging_allowed"] = False
    trace["optimization_problem"]["market"][0][1] = -5
    rows, _ = replay.perfect_foresight([trace], {0: observation(pv=0)}, 1, 50)
    assert rows[0]["charge_kwh"] == pytest.approx(0.6)


def test_perfect_foresight_bounds_negative_price_import_during_partial_slot_charge():
    pytest.importorskip("scipy")
    trace = trace_at(0)
    trace["optimization_problem"]["market"][0][1] = -5
    eta = trace["optimization_problem"]["constraints"]["round_trip_efficiency"] ** 0.5
    rows, _ = replay.perfect_foresight(
        [trace],
        {0: observation(house=0, pv=0.4)},
        6 - 0.2 * eta,
        50,
    )
    assert rows[0]["charge_kwh"] == pytest.approx(0.2)
    assert rows[0]["grid_import_kwh"] == pytest.approx(0.2 * (1 - 0.4 / 0.6))
    assert rows[0]["export_kwh"] == pytest.approx(0.4 - 0.2 + 0.2 / 3)


def test_perfect_foresight_respects_disabled_pv_when_battery_has_priority_over_ev():
    pytest.importorskip("scipy")
    trace = trace_at(0)
    trace["optimization_problem"]["market"][0][1] = -5
    trace["optimization_problem"]["commercial_policy"]["pv_charging_allowed"] = False
    trace["optimization_problem"]["ev_policy"]["pv_to_ev_first"] = False
    rows, _ = replay.perfect_foresight(
        [trace], {0: observation(house=0, pv=0.4, ev=0.5)}, 1, 50
    )
    assert rows[0]["charge_kwh"] == 0
    assert rows[0]["grid_import_kwh"] == pytest.approx(0.1)


def test_rolling_run_supplies_its_own_soc_to_each_new_plan(monkeypatch):
    traces = [trace_at(i * replay.SLOT_MS) for i in range(2)]
    actuals = {
        i * replay.SLOT_MS: observation(start=i * replay.SLOT_MS) for i in range(2)
    }
    bundles = [
        problem([10.0, 40.0], loads=[0.1, 0.2], start_ms=i * replay.SLOT_MS).forecast
        for i in range(2)
    ]
    observed = []

    class Planner:
        def optimize(self, candidate):
            observed.append(candidate.battery.soc_pct)
            return plan_with_budget(start=candidate.market[0].slot.start_ms)

    monkeypatch.setattr(replay, "DynamicProgrammingOptimizer", Planner)
    before = copy.deepcopy(traces)
    rows, _ = replay.run_strategy(
        "production",
        traces,
        actuals,
        bundles,
        [SimpleNamespace(scenarios=None)] * 2,
        4.8,
        None,
    )
    assert observed[0] == pytest.approx(80)
    assert observed[1] == pytest.approx(100 * rows[0]["end_energy_kwh"] / 6)
    assert observed[1] < observed[0]
    assert traces == before

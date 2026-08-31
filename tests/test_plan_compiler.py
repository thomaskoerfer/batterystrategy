"""Contract and architecture tests for the pure plan compiler."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    BatteryPlan,
    BatteryPlanSlot,
    PlanCompilationState,
    PlanMode,
    SlotKey,
    SlotProgress,
)
from custom_components.battery_strategy.plan_compiler import (
    DeterministicPlanCompiler,
)

SLOT_MS = 15 * 60 * 1000


def plan_slot(
    *,
    pv_charge_kwh: float = 0.0,
    grid_charge_kwh: float = 0.0,
    required_charge_kwh: float = 0.0,
    discharge_kwh: float = 0.0,
    discharge_budget_kwh: float = 0.0,
) -> BatteryPlanSlot:
    slot = SlotKey(0, SLOT_MS)
    charge_kwh = pv_charge_kwh + grid_charge_kwh
    mode = PlanMode.IDLE
    if charge_kwh > 0.0:
        mode = PlanMode.CHARGE
    elif discharge_kwh > 0.0:
        mode = PlanMode.DISCHARGE
    return BatteryPlanSlot(
        slot=slot,
        mode=mode,
        pv_charge_allowed=True,
        grid_charge_allowed=True,
        planned_charge_kwh=charge_kwh,
        planned_discharge_kwh=discharge_kwh,
        required_charge_kwh=required_charge_kwh,
        discharge_budget_kwh=discharge_budget_kwh,
        expected_soc_start_pct=50.0,
        expected_soc_end_pct=50.0,
        planned_pv_charge_kwh=pv_charge_kwh,
        planned_grid_charge_kwh=grid_charge_kwh,
    )


def battery_plan(slot: BatteryPlanSlot) -> BatteryPlan:
    return BatteryPlan(
        plan_id="plan-1",
        problem_id="problem-1",
        generated_at_ms=0,
        optimizer_version="test-v1",
        constraints=BatteryConstraints(
            capacity_kwh=6.0,
            min_soc_pct=10.0,
            max_soc_pct=100.0,
            max_charge_power_w=2400.0,
            max_discharge_power_w=2400.0,
            round_trip_efficiency=0.8,
        ),
        slots=(slot,),
        baseline_cost_eur=1.0,
        optimized_cost_eur=0.5,
    )


def compile_slot(
    slot: BatteryPlanSlot,
    *,
    charged_kwh: float = 0.0,
    discharged_kwh: float = 0.0,
    soc_pct: float = 50.0,
):
    directive, _state = DeterministicPlanCompiler().compile(
        battery_plan(slot),
        SlotProgress(slot.slot, charged_kwh, discharged_kwh, soc_pct),
        PlanCompilationState(),
        issued_at_ms=60_000,
    )
    return directive


def test_plan_compiler_module_has_no_runtime_or_io_dependencies():
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "battery_strategy"
        / "plan_compiler.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not node.names, "plan compiler must not use absolute imports"
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "") == "__future__"


def test_mixed_grid_charge_publishes_only_explicit_required_commitment():
    directive = compile_slot(
        plan_slot(
            pv_charge_kwh=0.125,
            grid_charge_kwh=0.3,
            required_charge_kwh=0.425,
        ),
        charged_kwh=0.1,
    )

    assert directive.pv_charge_allowed
    assert directive.grid_charge_allowed
    assert directive.required_charge_remaining_kwh == pytest.approx(0.325)
    assert directive.max_pv_charge_power_w == 2400.0
    assert directive.max_grid_charge_power_w == 2400.0
    assert directive.discharge_budget_remaining_kwh == 0.0


def test_pv_only_charge_never_authorizes_grid_charge():
    directive = compile_slot(plan_slot(pv_charge_kwh=0.2))

    assert directive.pv_charge_allowed
    assert not directive.grid_charge_allowed
    assert directive.required_charge_remaining_kwh == 0.0
    assert directive.max_grid_charge_power_w == 0.0
    assert directive.max_discharge_power_w == 2400.0


def test_discharge_progress_only_reduces_optimizer_budget():
    slot = plan_slot(discharge_kwh=0.2, discharge_budget_kwh=0.6)

    first = compile_slot(slot, discharged_kwh=0.1, soc_pct=50.0)
    later = compile_slot(slot, discharged_kwh=0.25, soc_pct=50.0)

    assert first.discharge_budget_remaining_kwh == pytest.approx(0.5)
    assert later.discharge_budget_remaining_kwh == pytest.approx(0.35)
    assert later.max_discharge_power_w == 2400.0


def test_discharge_permission_is_capped_by_measured_available_soc():
    directive = compile_slot(
        plan_slot(discharge_kwh=0.2, discharge_budget_kwh=0.6),
        soc_pct=12.0,
    )

    assert directive.discharge_budget_remaining_kwh == pytest.approx(0.12)
    assert directive.min_soc_pct == 10.0


def test_compiler_does_not_infer_grid_permission_without_a_commitment():
    slot = replace(
        plan_slot(grid_charge_kwh=0.2, required_charge_kwh=0.2),
        grid_charge_allowed=False,
        planned_charge_kwh=0.0,
        planned_grid_charge_kwh=0.0,
        required_charge_kwh=0.0,
        mode=PlanMode.IDLE,
    )
    directive = compile_slot(slot)

    assert not directive.grid_charge_allowed
    assert directive.max_grid_charge_power_w == 0.0


def test_compiler_rejects_progress_for_a_slot_outside_the_plan():
    slot = plan_slot()
    other = SlotKey(SLOT_MS, 2 * SLOT_MS)

    with pytest.raises(ValueError, match="not present"):
        DeterministicPlanCompiler().compile(
            battery_plan(slot),
            SlotProgress(other, 0.0, 0.0, 50.0),
            PlanCompilationState(),
            issued_at_ms=60_000,
        )


def test_compiler_is_deterministic():
    slot = plan_slot(discharge_kwh=0.2, discharge_budget_kwh=0.6)
    plan = battery_plan(slot)
    progress = SlotProgress(slot.slot, 0.0, 0.1, 50.0)
    compiler = DeterministicPlanCompiler()

    assert compiler.compile(
        plan, progress, PlanCompilationState(), 60_000
    ) == compiler.compile(
        plan, progress, PlanCompilationState(), 60_000
    )


def test_reoptimization_may_lower_but_not_raise_active_slot_commitments():
    compiler = DeterministicPlanCompiler()
    initial_slot = plan_slot(
        grid_charge_kwh=0.4,
        required_charge_kwh=0.4,
        discharge_budget_kwh=0.4,
    )
    progress = SlotProgress(initial_slot.slot, 0.1, 0.1, 50.0)
    _directive, state = compiler.compile(
        battery_plan(initial_slot),
        progress,
        PlanCompilationState(),
        60_000,
    )

    raised_slot = replace(
        initial_slot,
        planned_charge_kwh=0.6,
        planned_grid_charge_kwh=0.6,
        required_charge_kwh=0.6,
        discharge_budget_kwh=0.6,
    )
    raised_plan = replace(
        battery_plan(raised_slot),
        plan_id="plan-raised",
        problem_id="problem-raised",
    )
    raised, raised_state = compiler.compile(raised_plan, progress, state, 120_000)

    assert raised.required_charge_remaining_kwh == pytest.approx(0.3)
    assert raised.discharge_budget_remaining_kwh == pytest.approx(0.3)
    assert raised_state.required_charge_commitment_kwh == pytest.approx(0.4)
    assert raised_state.discharge_budget_commitment_kwh == pytest.approx(0.4)

    lowered_slot = replace(
        initial_slot,
        planned_charge_kwh=0.2,
        planned_grid_charge_kwh=0.2,
        required_charge_kwh=0.2,
        discharge_budget_kwh=0.2,
    )
    lowered_plan = replace(
        battery_plan(lowered_slot),
        plan_id="plan-lowered",
        problem_id="problem-lowered",
    )
    lowered, lowered_state = compiler.compile(
        lowered_plan,
        progress,
        raised_state,
        180_000,
    )

    assert lowered.required_charge_remaining_kwh == pytest.approx(0.1)
    assert lowered.discharge_budget_remaining_kwh == pytest.approx(0.1)
    assert lowered_state.required_charge_commitment_kwh == pytest.approx(0.2)
    assert lowered_state.discharge_budget_commitment_kwh == pytest.approx(0.2)


def test_new_slot_accepts_the_latest_plan_commitment():
    compiler = DeterministicPlanCompiler()
    first_slot = plan_slot(discharge_kwh=0.2, discharge_budget_kwh=0.2)
    _directive, state = compiler.compile(
        battery_plan(first_slot),
        SlotProgress(first_slot.slot, 0.0, 0.1, 50.0),
        PlanCompilationState(),
        60_000,
    )
    next_key = SlotKey(SLOT_MS, 2 * SLOT_MS)
    next_slot = replace(
        first_slot,
        slot=next_key,
        planned_discharge_kwh=0.5,
        discharge_budget_kwh=0.6,
    )
    next_plan = replace(
        battery_plan(first_slot),
        plan_id="plan-next",
        problem_id="problem-next",
        slots=(next_slot,),
    )

    directive, next_state = compiler.compile(
        next_plan,
        SlotProgress(next_key, 0.0, 0.0, 50.0),
        state,
        SLOT_MS + 60_000,
    )

    assert directive.discharge_budget_remaining_kwh == pytest.approx(0.6)
    assert next_state.committed_plan_id == "plan-next"

"""Phase-6 compiler adapter and shadow-parity tests."""

from __future__ import annotations

import datetime as dt

from custom_components.battery_strategy.const import (
    COMMAND_INPUT,
    COMMAND_OUTPUT,
    DISCHARGE_PRICE_SENSITIVE,
    GRID_CHARGING_PRICE_SENSITIVE,
    PV_CHARGING_ON,
)
from custom_components.battery_strategy.contracts import (
    PlanCompilationState,
    SlotProgress,
)
from custom_components.battery_strategy.coordinator import BatteryStrategyCoordinator
from custom_components.battery_strategy.models import StrategyInputs, StrategyOptions
from custom_components.battery_strategy.plan_compiler import (
    DeterministicPlanCompiler,
)
from custom_components.battery_strategy.plan_compiler_adapter import (
    closed_published_directive,
    contract_plan_from_strategy_plan,
    published_directive_from_contract,
)
from custom_components.battery_strategy.plan_models import PlanPoint, StrategyPlan


def _point(
    start_ms: int,
    *,
    charge_w: int = 0,
    pv_charge_w: int = 0,
    grid_charge_w: int = 0,
    required_charge_w: int = 0,
    discharge_w: int = 0,
    discharge_budget_kwh: float = 0.0,
) -> PlanPoint:
    return PlanPoint(
        ts_ms=start_ms,
        date="2026-09-02",
        price_ct=20.0,
        load_fc_w=800,
        pv_fc_w=300,
        grid_import_fc_w=500,
        grid_export_fc_w=0,
        grid_net_fc_w=500,
        mode=(COMMAND_INPUT if charge_w else COMMAND_OUTPUT if discharge_w else "idle"),
        power_w=max(charge_w, discharge_w),
        charge_fc_w=charge_w,
        discharge_fc_w=discharge_w,
        soc_pct=50.0,
        discharge_budget_kwh=discharge_budget_kwh,
        pv_charge_fc_w=pv_charge_w,
        grid_charge_fc_w=grid_charge_w,
        required_charge_fc_w=required_charge_w,
    )


def _options() -> StrategyOptions:
    return StrategyOptions(
        pv_charging=PV_CHARGING_ON,
        grid_charging=GRID_CHARGING_PRICE_SENSITIVE,
        discharge=DISCHARGE_PRICE_SENSITIVE,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        battery_capacity_kwh=6.0,
        max_charge_power_w=2400.0,
        max_discharge_power_w=2400.0,
    )


def test_compiler_adapter_publishes_grid_charge_directive():
    start_ms = int(
        dt.datetime(2026, 9, 2, 12, tzinfo=dt.timezone.utc).timestamp() * 1000
    )
    plan = StrategyPlan(
        points=[
            _point(
                start_ms,
                charge_w=1700,
                pv_charge_w=500,
                grid_charge_w=1200,
                required_charge_w=1700,
            )
        ],
        current_mode=COMMAND_INPUT,
        current_power_w=1700,
        reason="test",
    )
    options = _options()
    contract_plan = contract_plan_from_strategy_plan(plan, options, start_ms)
    compiled, _ = DeterministicPlanCompiler().compile(
        contract_plan,
        SlotProgress(contract_plan.slots[0].slot, 0.0, 0.0, 50.0),
        PlanCompilationState(),
        start_ms,
    )
    candidate = published_directive_from_contract(compiled, plan, options)

    assert candidate.grid_charge_allowed
    assert candidate.must_charge_w == 1700
    assert candidate.must_charge_remaining_kwh == 0.425
    assert candidate.discharge_budget_kwh == 0.0


def test_compiler_adapter_preserves_discharge_progress_and_budget():
    start_ms = int(
        dt.datetime(2026, 9, 2, 18, tzinfo=dt.timezone.utc).timestamp() * 1000
    )
    plan = StrategyPlan(
        points=[
            _point(
                start_ms,
                discharge_w=1200,
                discharge_budget_kwh=0.6,
            )
        ],
        current_mode=COMMAND_OUTPUT,
        current_power_w=1200,
        reason="test",
    )
    options = _options()
    contract_plan = contract_plan_from_strategy_plan(plan, options, start_ms)
    compiled, _ = DeterministicPlanCompiler().compile(
        contract_plan,
        SlotProgress(contract_plan.slots[0].slot, 0.0, 0.2, 50.0),
        PlanCompilationState(),
        start_ms + 300_000,
    )
    candidate = published_directive_from_contract(compiled, plan, options)
    assert candidate.discharge_budget_kwh == 0.4
    assert candidate.must_charge_w == 0


def test_closed_directive_has_no_automatic_permissions():
    directive = closed_published_directive(_options(), slot_start_ms=1_800_000_000_000)

    assert not directive.pv_charge_allowed
    assert not directive.grid_charge_allowed
    assert directive.must_charge_w == 0
    assert directive.must_charge_remaining_kwh == 0.0
    assert directive.discharge_budget_kwh == 0.0


def _cutover_coordinator() -> BatteryStrategyCoordinator:
    coordinator = object.__new__(BatteryStrategyCoordinator)
    coordinator._plan_compiler = DeterministicPlanCompiler()
    coordinator._plan_compilation_state = PlanCompilationState()
    coordinator._plan_compiler_error = None
    coordinator._slot_charged_kwh = 0.0
    coordinator._slot_discharged_kwh = 0.0
    coordinator._compiler_progress_reconstructable = True
    coordinator._compiler_snapshot_dirty = False
    return coordinator


def _inputs(*, soc_pct: float = 50.0) -> StrategyInputs:
    return StrategyInputs(500.0, 0.0, 0.0, 0.0, soc_pct=soc_pct)


def test_cutover_compiler_owns_progress_and_replan_latch():
    start_ms = 1_800_000_000_000
    first_plan = StrategyPlan(
        [_point(start_ms, discharge_w=1200, discharge_budget_kwh=0.6)],
        COMMAND_OUTPUT,
        1200,
        "first",
    )
    reduced_plan = StrategyPlan(
        [_point(start_ms, discharge_w=800, discharge_budget_kwh=0.2)],
        COMMAND_OUTPUT,
        800,
        "reduced",
    )
    coordinator = _cutover_coordinator()

    first = coordinator._compile_authoritative_directive(
        first_plan, _options(), _inputs(), start_ms
    )
    coordinator._slot_discharged_kwh = 0.1
    reduced = coordinator._compile_authoritative_directive(
        reduced_plan, _options(), _inputs(), start_ms + 60_000
    )
    reopened = coordinator._compile_authoritative_directive(
        first_plan, _options(), _inputs(), start_ms + 120_000
    )

    assert first.discharge_budget_kwh == 0.6
    assert reduced.discharge_budget_kwh == 0.1
    assert reopened.discharge_budget_kwh == 0.1


def test_cutover_compiler_fails_closed_without_plan():
    coordinator = _cutover_coordinator()
    plan = StrategyPlan([], "idle", 0, "missing")

    directive = coordinator._compile_authoritative_directive(
        plan, _options(), _inputs(), 1_800_000_000_000
    )

    assert coordinator._plan_compiler_error == "no_plan"
    assert not directive.pv_charge_allowed
    assert not directive.grid_charge_allowed
    assert directive.discharge_budget_kwh == 0.0

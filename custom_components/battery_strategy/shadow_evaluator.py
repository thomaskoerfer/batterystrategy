"""Pure, non-authoritative scenario evaluation for the shadow rollout."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from .contracts import (
    BatteryPlan,
    OptimizationProblem,
    OptimizationResult,
    ScenarioBuildRequest,
    ScenarioBuildResult,
)
from .economic_optimizer import UnifiedScenarioOptimizer
from .scenario_generation import ScenarioBuilder


@dataclass(frozen=True, slots=True)
class ShadowEvaluation:
    status: str
    runtime_ms: float
    problem: OptimizationProblem | None
    build_result: ScenarioBuildResult | None
    optimization_result: OptimizationResult | None
    plan: BatteryPlan | None
    optimizer_diagnostics: dict[str, object]
    error: Exception | None = None


def evaluate_shadow(
    scenario_request: ScenarioBuildRequest | None,
    optimization_problem: OptimizationProblem | None,
) -> ShadowEvaluation:
    """Evaluate one captured vintage without persistence or HA dependencies."""
    started = time.perf_counter()
    if scenario_request is None:
        return _result("scenario_inputs_unavailable", started, optimization_problem)
    try:
        build_result = ScenarioBuilder().build(scenario_request)
        if optimization_problem is None:
            status = (
                build_result.status.value
                if build_result.scenarios is None
                else "optimization_problem_unavailable"
            )
            return _result(status, started, None, build_result=build_result)
        problem = (
            replace(optimization_problem, scenarios=build_result.scenarios)
            if build_result.scenarios is not None
            else optimization_problem
        )
        optimizer = UnifiedScenarioOptimizer()
        optimization_result = optimizer.optimize(problem)
        projection = optimization_result.projection
        plan = BatteryPlan(
            projection.projection_id,
            projection.problem_id,
            projection.generated_at_ms,
            projection.optimizer_version,
            projection.constraints,
            projection.slots,
            projection.baseline_cost_eur,
            projection.optimized_cost_eur,
        )
        status = (
            "completed"
            if build_result.scenarios is not None
            else "completed_p50_fallback"
        )
        return _result(
            status,
            started,
            problem,
            build_result=build_result,
            optimization_result=optimization_result,
            plan=plan,
            optimizer_diagnostics=dict(optimizer.last_diagnostics),
        )
    except Exception as err:
        return _result("optimizer_failed", started, optimization_problem, error=err)


def _result(status, started, problem, **kwargs):
    return ShadowEvaluation(
        status=status,
        runtime_ms=round((time.perf_counter() - started) * 1000.0, 3),
        problem=problem,
        build_result=kwargs.get("build_result"),
        optimization_result=kwargs.get("optimization_result"),
        plan=kwargs.get("plan"),
        optimizer_diagnostics=kwargs.get("optimizer_diagnostics", {}),
        error=kwargs.get("error"),
    )

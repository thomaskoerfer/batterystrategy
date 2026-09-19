"""Build explicit optimizer contracts from an already captured input snapshot."""

from __future__ import annotations

from dataclasses import replace

from .contracts import (
    BatteryConstraints,
    BatteryPlan,
    BatteryState,
    CommercialPolicy,
    EvInteractionPolicy,
    ForecastBundle,
    MarketSlot,
    OptimizationProblem,
)
from .economic_optimizer import (
    ENERGY_STEP_KWH,
    DynamicProgrammingOptimizer,
    StochasticDynamicProgrammingOptimizer,
)

STOCHASTIC_MAX_FINE_STATES = 1200
# HA's ten-second coordinator cannot capture at exactly :00. Treat its first
# completed cycle after a quarter-hour boundary as the boundary decision while
# rejecting genuine mid-slot replans.
STOCHASTIC_BOUNDARY_TOLERANCE_MS = 30_000


def build_optimization_problem(
    *,
    intervals,
    forecast: ForecastBundle,
    start_energy_kwh: float,
    constraints: BatteryConstraints,
    policy: CommercialPolicy,
    evaluated_at_ms: int,
    ev_policy: EvInteractionPolicy = EvInteractionPolicy(),
) -> OptimizationProblem:
    """Return one immutable problem from normalized market and forecast data."""
    as_of_ms = max(
        evaluated_at_ms,
        forecast.load.generated_at_ms,
        forecast.pv.generated_at_ms,
    )
    return OptimizationProblem(
        problem_id=(
            f"plan:{evaluated_at_ms}:{forecast.load.forecast_id}:"
            f"{forecast.pv.forecast_id}"
        ),
        as_of_ms=as_of_ms,
        forecast=forecast,
        market=tuple(
            MarketSlot(
                load_slot.slot,
                interval.price_eur_per_kwh * 100.0,
                policy.export_opportunity_ct_per_kwh,
                "captured_market_snapshot",
            )
            for interval, load_slot in zip(intervals, forecast.load.slots, strict=True)
        ),
        battery=BatteryState(
            as_of_ms,
            max(
                constraints.min_soc_pct,
                min(
                    constraints.max_soc_pct,
                    100.0 * start_energy_kwh / constraints.capacity_kwh,
                ),
            ),
        ),
        constraints=constraints,
        policy=policy,
        ev_policy=ev_policy,
    )


def optimize_snapshot(**kwargs) -> tuple[OptimizationProblem, BatteryPlan, dict]:
    """Optimize one captured snapshot and retain its auditable input contract."""
    problem = build_optimization_problem(**kwargs)
    deterministic = DynamicProgrammingOptimizer()
    if problem.forecast.scenarios is None:
        return problem, deterministic.optimize(problem), {"mode": "deterministic_p50"}
    if (
        problem.as_of_ms - problem.forecast.load.slots[0].slot.start_ms
        > STOCHASTIC_BOUNDARY_TOLERANCE_MS
    ):
        fallback = deterministic.optimize(problem)
        version = f"{fallback.optimizer_version}-stochastic-mid-slot-fallback"
        return (
            problem,
            replace(
                fallback,
                plan_id=f"{problem.problem_id}:{version}",
                optimizer_version=version,
            ),
            {"fallback_reason": "stochastic_mid_slot_guard"},
        )
    usable_energy_kwh = (
        problem.constraints.capacity_kwh
        * (problem.constraints.max_soc_pct - problem.constraints.min_soc_pct)
        / 100.0
    )
    if round(usable_energy_kwh / ENERGY_STEP_KWH) + 1 > STOCHASTIC_MAX_FINE_STATES:
        fallback = deterministic.optimize(problem)
        version = f"{fallback.optimizer_version}-stochastic-complexity-fallback"
        return (
            problem,
            replace(
                fallback,
                plan_id=f"{problem.problem_id}:{version}",
                optimizer_version=version,
            ),
            {"fallback_reason": "stochastic_complexity_guard"},
        )
    try:
        plan, diagnostics = (
            StochasticDynamicProgrammingOptimizer().optimize_with_diagnostics(problem)
        )
        return problem, plan, diagnostics
    except Exception:
        fallback = deterministic.optimize(problem)
        version = f"{fallback.optimizer_version}-stochastic-fallback"
        return (
            problem,
            replace(
                fallback,
                plan_id=f"{problem.problem_id}:{version}",
                optimizer_version=version,
            ),
            {"fallback_reason": "stochastic_optimizer_error"},
        )

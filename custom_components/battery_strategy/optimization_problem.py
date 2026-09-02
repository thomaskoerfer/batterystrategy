"""Build explicit optimizer contracts from an already captured input snapshot."""

from __future__ import annotations

from .contracts import (
    BatteryConstraints,
    BatteryPlan,
    BatteryState,
    CommercialPolicy,
    ForecastBundle,
    MarketSlot,
    OptimizationProblem,
)
from .economic_optimizer import DynamicProgrammingOptimizer


def build_optimization_problem(
    *,
    intervals,
    forecast: ForecastBundle,
    start_energy_kwh: float,
    constraints: BatteryConstraints,
    policy: CommercialPolicy,
    evaluated_at_ms: int,
) -> OptimizationProblem:
    """Return one immutable problem from normalized market and forecast data."""
    return OptimizationProblem(
        problem_id=(
            f"plan:{evaluated_at_ms}:{forecast.load.forecast_id}:"
            f"{forecast.pv.forecast_id}"
        ),
        as_of_ms=max(
            evaluated_at_ms,
            forecast.load.generated_at_ms,
            forecast.pv.generated_at_ms,
        ),
        forecast=forecast,
        market=tuple(
            MarketSlot(
                load_slot.slot,
                float(interval["price_eur"]) * 100.0,
                policy.export_opportunity_ct_per_kwh,
                "captured_market_snapshot",
            )
            for interval, load_slot in zip(
                intervals, forecast.load.slots, strict=True
            )
        ),
        battery=BatteryState(
            evaluated_at_ms,
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
    )


def optimize_snapshot(**kwargs) -> tuple[OptimizationProblem, BatteryPlan]:
    """Optimize one captured snapshot and retain its auditable input contract."""
    problem = build_optimization_problem(**kwargs)
    return problem, DynamicProgrammingOptimizer().optimize(problem)

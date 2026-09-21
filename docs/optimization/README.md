# Optimization

## Purpose

Optimization converts prices, coherent load/PV/EV scenarios, battery state and
physical/commercial constraints into one current decision and one future
projection. It decides energy allocation; it never follows a live meter or
writes hardware.

## Interface

```text
UnifiedScenarioOptimizer.optimize(OptimizationProblem) -> OptimizationResult
```

`OptimizationResult` has two independent outputs:

- `OptimizationDecision`: executable permission for the current quarter-hour;
- `PlanProjection`: a non-authoritative P50 trajectory for operators, cost
  estimates and evaluation.

The compiler may consume only the decision. A dashboard or persisted projection
can never recreate executable intent.

The optimizer is deterministic and side-effect free. It has no Home Assistant,
history, persistence, filesystem, network or wall-clock dependency.

## Scenario model

All coherent paths share one first-slot charge requirement and discharge
budget. Each path then has independent recourse. Scenario probabilities weight
the objective. If a scenario bundle is unavailable, the forecast P50 path is
wrapped as one probability-one scenario and processed by exactly the same
algorithm; there is no second point-forecast optimizer.

The future operator projection follows that probability-one P50 path. It is not
a promise that later actions will execute: every quarter-hour is reoptimized
from measured SoC and current evidence.

## Economic model

The lexicographic objective minimizes:

1. expected grid-import cost minus actual export revenue, battery throughput
   margin and terminal inventory value;
2. PV export when monetary cost is tied;
3. battery throughput when cost and export are tied.

Physical SoC, charge/discharge power, efficiency, source permissions and EV
interaction are hard constraints. Expected PV spill therefore creates headroom
without a separate recovery credit. There is no price-rank discharge floor,
PV-recovery confidence/reserve or future-maximum-price charge gate.

Terminal value is an explicit horizon-boundary assumption. It is recorded with
the optimization problem and evaluated independently; it must not be replaced
by hidden price ranks.

## Plan semantics

The current decision carries source-specific charge permission, required grid
charge and a commercial discharge budget. Budget is energy permission, not a
power target. The live controller still limits discharge to eligible measured
load and never turns a projection into permission.

The discharge budget is a commercial envelope, calculated independently of
the expected current-slot load. For every candidate permission, optimization
compares avoided current import with the continuation value of the resulting
lower inventory. The envelope ends at the first candidate with economic
regret, so later peaks, partial or complete recharge, charge power, efficiency,
PV spill and terminal inventory are valued by one continuation model. Unused
permission has no throughput cost. Expected discharge remains load-dependent
and cannot exceed the envelope.

Projection slots contain expected charge/discharge sources and SoC solely for
presentation and evaluation.

## Rollout

The first release is observational: the established optimizer remains the sole
live authority, while the unified optimizer runs after publication and writes
only bounded evaluation data. The cutover release makes
`OptimizationDecision` authoritative and removes the old optimizer and
plan-shaped shadow compatibility data. See the approved impact analysis in
`docs/impact-analyses/2026-09-20-unified-scenario-optimizer.md`.

## Verification

Tests cover P50 degradation through the same core, common first actions,
scenario recourse, RTE, uneconomic cycles, negative prices, terminal inventory,
PV spill, EV exclusion and deterministic tie-breaking. Retained-history replay
compares decision regret and projection quality with perfect foresight.

## Setup independence

Inputs use normalized contracts only. No rule contains household identifiers,
entity IDs, provider accounts, addresses, serial numbers or private endpoints.

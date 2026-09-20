# Unified Scenario Optimizer Contract Change

Status: **Approved** on 2026-09-20. Implementation and observation are pending.

## Decision

Optimization has one implementation for both uncertain and point forecasts.
An absent scenario ensemble is represented by one probability-one P50 path;
it is not handled by a second algorithm.

The optimizer returns two deliberately different contracts:

- `OptimizationDecision` is the executable commercial permission for the
  current quarter-hour only.
- `PlanProjection` is a non-authoritative future trajectory for operators,
  costs and evaluation.

The compiler consumes only `OptimizationDecision`. It never derives permission
from `PlanProjection`.

PV headroom is valued by the scenario objective and physical constraints.
`discharge_floor_ct_per_kwh`, `pv_recovery_confidence` and
`pv_recovery_reserve_kwh` are removed. There is no separate recovery credit,
price-rank floor or future-peak charge gate.

## Objective

The optimizer minimizes expected import cost minus export revenue plus the
configured battery throughput margin and terminal inventory value. One
first-slot policy is common to all scenarios; future actions are scenario
recourse. Exact cost ties prefer less PV export and then less battery
throughput. This ordering is deterministic.

## Learning boundary

Scenario and optimization vintages are written to a bounded observational
ledger. The ledger cannot be read by planning, compilation, live control or
actuation. A future offline trainer may create a reviewed, immutable model
snapshot; online reinforcement learning is explicitly outside this change.

## Producer and consumer impact

- Scenario generation remains the sole owner of coherent path construction.
- Optimization produces `OptimizationResult` containing the decision and
  projection.
- Planning publishes both without converting projection data back into intent.
- The compiler changes from a horizon plan input to the current decision.
- Evaluation records decisions, projections, scenarios and candidate evidence.
- Home Assistant presentation reads only the projection.

## Rollout

1. Shadow release: the new contracts, optimizer and ledger run after the
   existing authoritative plan. They have no compiler or actuator reference.
2. Cutover release: the new decision becomes authoritative and the old
   optimizer, plan contract and shadow comparison are removed.

Rollback from cutover uses the last production release and its persisted plan
schema. A generation marker prevents a plan from another generation being
executed after rollback or upgrade.

## Verification

- Contract invariants and serialization round trips.
- Golden economic cases, P50 degradation, scenario recourse, PV spill,
  uneconomic cycles, terminal inventory and deterministic tie-breaking.
- Shadow/perfect-foresight comparison over retained history.
- Architecture tests proving that the ledger is write-only from evaluation and
  that projection cannot reach compilation.
- Full unit suite, HACS validation and hassfest before deployment.

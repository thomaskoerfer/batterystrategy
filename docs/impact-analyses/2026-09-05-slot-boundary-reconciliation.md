# Impact analysis: slot-boundary discharge reconciliation

## Decision

The owner approved a narrow extension of the optimization-to-compiler contract
on 2026-09-05. A future-slot discharge commitment obtained from a plan generated
before that slot starts is provisional. The first plan generated at or after the
boundary may replace it once; the commitment is final thereafter.

## Motivation

The former one-minute forced prefetch optimized the almost elapsed current slot
as if a complete 15-minute interval remained. This could consume projected
energy in the model, publish zero budget for the next slot and latch that zero at
the boundary. The first plan based on the real boundary SoC then proposed a
higher budget, but the compiler's no-increase rule rejected it.

## Contract impact

- `PlanCompilationState` persists an explicit provisional/final discharge phase.
- Only discharge budget receives one-time boundary reconciliation.
- Required grid-charge commitments keep their previous monotone rule.
- Measured slot discharge is always subtracted from the reconciled total.
- Reconciliation requires a currently available real SoC; a bridged value may
  continue safe live control but may not seed a new economic plan or increase
  commercial permission.
- The plan generation timestamp must be at or after the latest transition back
  to a live SoC source. Current source availability alone is insufficient.
- After reload/recovery, a cached plan predating the restored live SoC waits for
  the already forced fresh run and leaves the commitment provisional.
- With an unavailable/bridged SoC, the first post-boundary plan can only reduce
  the budget and finalizes it conservatively.
- Mid-slot startup proration and pre-RC14 snapshots are final by default.
- No elapsed-seconds heuristic defines the reconciliation window.

The optimizer, forecasts, live meter following, PV-follow, EV policy and
actuator contracts are unchanged.

## Runtime impact

The separate pre-boundary forced optimizer run is removed. One refresh is forced
for each current slot at or after its boundary. The force key is persisted in
coordinator memory only after the planner accepts or queues the run, allowing a
failed input capture to retry.

The compiler-runtime store remains on major schema 1 and advances to minor
schema 2. Home Assistant's same-major migration keeps the additive phase field
backward readable; the decoder defaults absent fields to `final`.

## Verification

- A pre-boundary zero budget can be replaced once by a post-boundary budget.
- Already discharged energy reduces the replacement's remaining budget.
- A second same-slot increase is rejected while a reduction is accepted.
- Bridged SoC cannot authorize an increase.
- The phase survives persistence; old snapshots migrate to final.
- Mid-slot startup proration remains final.
- Invalid plans fail closed without erasing the active commitment.
- Operator mode/power follows the current cached slot and becomes idle/zero
  when no current point exists; this projection remains non-authoritative.

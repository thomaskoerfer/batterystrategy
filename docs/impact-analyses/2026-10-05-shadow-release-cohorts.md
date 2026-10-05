# Shadow optimizer release cohorts

Status: **Approved** on 2026-10-05 for shadow-only implementation and review.

## Problem

The retained optimizer report combined vintages from several economic
implementations because their trace schema and optimizer version stayed
unchanged. That made superseded conservative decisions count against the
current candidate. Tiny serialized charge values from floating-point arithmetic
could also make a valid idle policy fail perfect-foresight replay with infinite
reported regret.

## Decision

- Identify an evaluation cohort by trace schema, unified optimizer version and
  Scenario Builder model version.
- Evaluate only the homogeneous cohort declared by the evaluator's checked-out
  trace, optimizer and Scenario Builder versions. Report older trace
  availability, but never include it in readiness or regret metrics. A failed
  current cohort therefore cannot fall back to an older successful cohort.
- Start `scenario-dp-v3` as a fresh observation cohort.
- Use the Scenario Builder's canonical release identity instead of composing it
  independently in evaluation consumers.
- Persist the intended optimizer and Scenario Builder versions on every
  evaluation envelope, including failures, and include the Scenario Builder
  repair-policy version in the key.
- Include unexpected Scenario Builder and optimizer exceptions in their
  respective release success rates.
- Canonicalize policy values at or below `1e-9 kWh` to exact zero in the
  optimizer and retrospective replay.

The expected-cost objective, scenario probabilities, commercial discharge
envelope and all public contracts remain unchanged. Replaying current retained
vintages showed that the current optimizer already grants the physical maximum
when it is commercially preferred; adding an unmeasured aggressiveness rule
would therefore be incorrect.

## Boundaries and safety

This change is confined to the post-publication optimizer shadow and its
offline evaluator. The production optimizer, planning publication, compiler,
live controller and actuator remain untouched. No entity, option, migration or
state schema changes. Rollback is removal of this shadow-only commit.

## Verification

- Unit tests prove cohort isolation and exact-zero policy normalization.
- Optimizer regressions prove the common first decision remains deterministic.
- Full tests, architecture boundaries, HACS validation and hassfest must pass.
- The current cohort must collect a new complete observation window before any
  cutover decision.

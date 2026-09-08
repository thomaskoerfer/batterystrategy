# Impact analysis: mid-slot budget accounting

## Decision

The owner approved correction of a reproduced mid-slot accounting defect on
2026-09-08. A plan generated in an active slot starts from the current battery
SoC. Its current-slot charge and discharge amounts are therefore prospective
from that planning instant, while compiler commitments and measured progress
remain cumulative from the slot boundary.

## Defect

The compiler previously compared a post-boundary plan amount directly with its
cumulative slot commitment and then subtracted measured progress. This counted
progress twice. In the captured case, a `0.599 kWh` discharge commitment had
used `0.373 kWh`; a refreshed prospective budget of `0.409 kWh` incorrectly
left only `0.036 kWh` instead of preserving the existing `0.227 kWh` remainder.

## Correction

The runtime records charged and discharged slot progress at the exact input
snapshot of every accepted optimizer run. Before the existing lower-only
comparison, the compiler normalizes a post-boundary amount to cumulative slot
basis:

`candidate commitment = progress at plan snapshot + prospective plan amount`

It then applies the existing reconciliation and monotonic rules. This is
equivalent to comparing the previous remaining permission with the new
prospective permission. Pre-boundary plans remain total slot commitments.
Measured progress is still deducted exactly once when the live directive is
published.

The same normalization applies to required grid charge because it is produced
from the same current-SoC optimization horizon. Grid permission remains
strictly lower-only and cannot be newly opened inside a slot.

## Architectural impact

- Optimizer economics, forecasts, live control and actuation are unchanged.
- The pure compiler still receives only typed plans, progress and explicit
  state; it reads no prices, history, Home Assistant state or hardware.
- The checkpoint is bounded to the active slot and is not persisted. Restored
  plans without it are interpreted conservatively and cannot reopen a
  commitment.
- No persisted schema, entity, option or dashboard migration is required.
- Restart fail-closed behavior and physical SoC/power limits are unchanged.

## Verification and rollback

Regression coverage reproduces the observed values and checks charge,
discharge, provisional reconciliation, clean reload and lower-only behavior.
Rollback is the preceding release; no state migration is needed because the
stored commitment fields and units are unchanged.

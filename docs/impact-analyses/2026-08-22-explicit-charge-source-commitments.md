# Impact analysis: explicit charge-source commitments

## Problem

The beta-20 optimizer planned total battery charge on a 25 Wh stored-energy
lattice. Plan compilation reconstructed its grid component by subtracting
rounded forecast PV surplus. A mixed PV transition could therefore leave a
small paid remainder and turn it into `must_charge`, even though the economic
intent was opportunistic PV capture. The 2026-08-23 plan exposed 37-93 W
pre-noon commitments of this kind.

## Approved design

The optimizer is the sole owner of charge-source semantics. Every future slot
publishes planned PV charge, planned grid charge and required total charge. A
pure PV slot has zero required charge. A slot with a genuine grid commitment
publishes the total battery input that must be reached; realized PV supplies it
first and grid power fills only the remainder.

One stored-energy quantum is 25 Wh. At 92% one-way charge efficiency this is
about 27.2 Wh AC. A mixed transition with less paid energy than that quantum is
a lattice remainder rather than a standalone grid action. After economic path
selection, the optimizer may replace that slot with exact PV-only charge and
move the unchanged AC energy into an already required, cheaper grid slot. The
target must occur before the next planned discharge and have enough physical
charge capacity. If any condition fails, the earlier commitment remains. The
canonical SoC and flow trajectory is rebuilt from the moved energy before the
plan is published.

Earlier, more expensive grid charging remains legal when later cheaper slots
cannot supply enough energy within charge-power, SoC or deadline constraints.
Forecast load, forecast PV, confidence policy, round-trip efficiency, export
opportunity and minimum margin remain in the primary economic objective.

## Regression controls

- Explicit PV and grid source energy must sum to planned charge.
- Required charge cannot exist without a grid commitment and cannot exceed
  total planned charge.
- The reproduced morning PV remainder moves into an existing cheaper grid
  commitment and yields zero required charge before that window.
- A capacity-short counterexample retains an earlier required grid charge.
- Zero, forecast and excess realized PV all preserve the required total input;
  PV always displaces grid before grid energy is used.
- The beta-19 discharge-replacement incident and all existing uncertainty,
  PV-recovery, EV, progress and fail-safe tests remain mandatory.

## Rollout

This is a plan-contract change for `0.2.0-beta.21`. Beta 20 remains the live
rollback until the new candidate passes the complete suite and a current-plan
replay. Deployment requires the normal announced Home Assistant restart.

## Status

- Approved by owner: 2026-08-22
- Implemented locally: 2026-08-22
- Candidate replayed against the same live beta-20 input snapshot: 2026-08-22
  - pre-noon planned grid charge: 0.055 kWh -> 0.000 kWh
  - optimized tomorrow cost: 1.098 EUR -> 1.098 EUR
  - end-of-horizon SoC: 69.58% -> 69.58%
- Observed live: pending deployment

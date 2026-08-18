# Impact analysis: spill-backed PV recovery

## Reason and evidence

A low-SoC plan exposed a small discharge budget because its discretized charge
plan still forecast a few watts of grid export. The battery had ample physical
headroom, so the export was not evidence that later PV would be lost. Live used
the permission for a short discharge that reduced energy available for a more
valuable evening slot.

## Semantic impact

`discharge_budget_kwh` remains commercial permission rather than a live power
target. Its PV-recovery component is tightened: it is positive only when
confidence-weighted future PV surplus exceeds physical battery headroom plus
the existing uncertainty reserve. Planned grid export remains diagnostic and
cannot independently authorize discharge.

Units, signs, slot alignment and the plan schema do not change. This is a
compatible correction to optimizer semantics, not a contract-schema change.

## Dependency and decision impact

Only the optimizer's PV-recovery budget calculation changes. Forecast inputs,
the plan compiler, live controller, EV policy and actuator are unchanged.
Low-SoC plans may expose less discharge permission. Near-full plans with PV that
would otherwise be uncapturable retain bounded recovery permission.

## Verification

Regression coverage proves that:

- planned export with sufficient headroom does not open a recovery budget;
- optimizer plan rounding cannot authorize discharge;
- physical headroom shortage produces the expected bounded AC budget;
- real forecast PV spill still opens a budget;
- PV-recovery remains disabled when PV charging is disabled;
- using a valid headroom budget does not trigger meaningful grid repurchase;
- existing higher-value load reservation and live PV priority tests remain
  green.

## Rollout and rollback

Release `0.2.0-beta.12` changes only optimizer code and documentation. The
previous tagged release `0.2.0-beta.11` is the rollback point. Restoring that
integration version and restarting Home Assistant restores the former budget
semantics without data migration.

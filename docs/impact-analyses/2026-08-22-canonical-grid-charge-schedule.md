# Impact analysis: canonical grid-charge schedule

## Reason and evidence

On 2026-08-22 the published future-slot table repeatedly showed up to 0.6 kWh
of planned grid charging in the current slot while the live directive exposed
zero required charge. From 12:30 through 14:45 local time the optimizer planned
input in every quarter-hour, but `must_charge` remained zero. It became active
only from 15:00 to 15:13 and briefly from 15:15 to 15:20 before manual charging
started.

The plan compiler deliberately deferred current planned grid energy whenever
later equal-or-lower-priced slots had theoretical capacity. That retained useful
optionality but created two schedules: the canonical optimizer table and an
unpublished live deadline schedule. Planned SoC therefore described actions the
live controller intentionally did not execute.

## Approved semantics

The owner approved the correction on 2026-08-22. Deferral remains supported,
but the optimizer owns it. Among plans with identical real monetary cost it
first prefers less grid energy and then places necessary grid charging as late
as feasible. This is a lexicographic tie-break, not a fictional price adjustment.

The primary objective and all inputs remain authoritative: load forecast, PV
forecast, PV confidence and recovery reserve, prices, round-trip efficiency,
feed-in opportunity, battery constraints and minimum margin. A later schedule
can never beat a plan with lower primary economic cost. Deferral preserves
optionality for forecast updates and realized PV rather than weakening forecast
or uncertainty handling.

Once published, the current plan slot is executable. Its planned grid component
becomes required live charge and is reduced only by measured slot progress. The
plan compiler no longer moves energy to another slot. Live PV remains first: it
supplies the required battery input before grid power fills the remaining gap,
and excess realized PV may still charge according to the existing live policy.

## Contract and dependency impact

The `BatteryPlan` and `PlanLiveDirective` schemas, units and signs are unchanged.
Optimizer tie resolution and plan-compiler semantics change together. Future
plan charge, planned SoC and current `must_charge` now describe one schedule.

Forecasting, feature collection, PV-recovery confidence, discharge budgets,
EV policy, meter following, slot progress, actuator translation and hardware
limits are unchanged. No new configuration or persistence migration is needed.

## Verification

Regression coverage proves that:

- a more expensive plan is never chosen merely because it charges later;
- equal-cost plans prefer less grid input and then later grid input;
- the optimizer publishes one necessary charge in the last feasible equal-price
  slot instead of publishing it earlier and hiding a second deadline schedule;
- the 2026-08-22 incident shape shows zero early cheap-slot charge followed by
  the late charge sequence required before expensive load;
- current published grid charge becomes `must_charge` even when later equal-price
  slots retain capacity;
- measured progress reduces remaining required energy;
- live PV supplies required input before grid power, and realized PV above the
  required rate does not create unnecessary grid import;
- existing forecast, PV-spill, recharge-reserve, EV and safety tests remain green.

## Rollout and rollback

The correction is added to the not-yet-deployed `0.2.0-beta.20` candidate. It
requires the normal announced Home Assistant restart and post-restart comparison
of the future-slot table, current directive, command trace and SoC progression.
Rollback restores the previously deployed beta and restarts Home Assistant; no
data migration is involved beyond the separate Phase-3 feature-store migration.

## Status

- Proposed: 2026-08-22
- Approved by owner: 2026-08-22
- Implemented locally: 2026-08-22
- Observed: pending deployment and live plan review

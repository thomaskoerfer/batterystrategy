# Plan compiler semantics

This document defines how an economic `BatteryPlan` becomes the directive used
by fast live control. It is normative together with `INTERFACE_CONTRACTS.md` and
the executable contracts in `custom_components/battery_strategy/contracts`.

The behavior below was approved by the repository owner on 2026-08-31. Changing
it requires an impact analysis and renewed explicit approval.

## Responsibility

The optimizer decides the commercial value of charging and discharging. The
plan compiler does not read prices, forecasts, Home Assistant, history or the
wall clock. It receives only:

- the immutable `BatteryPlan`;
- measured progress in the active slot;
- explicit compilation state from the previous invocation;
- the caller-supplied issue timestamp.

It returns one `PlanLiveDirective` and the next immutable compilation state. It
may translate and reduce optimizer permission, but it never creates economic
permission, moves energy between slots or changes a future SoC trajectory.
The directive carries both remaining required energy and the latched required
charge power; live control never reconstructs that rate from dashboard data.

The compiler receives `PlanningResult.battery_plan` directly. It must never
reconstruct a plan from `StrategyPlan`, dashboard profiles or diagnostics. A
missing canonical plan is a closed commercial directive.

## A slot is a commitment

At the start of each UTC-aligned 15-minute slot, the compiler latches the latest
economic directive for that slot. This prevents rolling optimizer runs from
causing charge or discharge permission to jump repeatedly inside the interval.

For the active slot:

- actual charged energy reduces remaining required charge;
- actual discharged energy reduces remaining commercial discharge budget;
- a later optimizer plan may lower or withdraw an economic commitment;
- a later optimizer plan may not increase or newly open that commitment;
- physical SoC and power limits may always reduce executable permission;
- increases and new economic choices apply from the next slot.

At the next slot boundary, the latest valid plan is latched without carrying the
previous slot's economic ceiling forward.

### Example

The 20:00 slot opens with `0.40 kWh` discharge permission. By 20:05 the battery
has discharged `0.10 kWh`, leaving `0.30 kWh`.

- a rolling replan proposes `0.60 kWh`: remaining permission stays `0.30 kWh`;
- a rolling replan proposes `0.20 kWh`: remaining permission becomes `0.10 kWh`;
- at 20:15, the latest plan for the new slot is accepted in full.

The optimizer still starts every new calculation from the measured current SoC.
Its changed current-slot result may withdraw permission, while its complete
result updates all future slots. This deliberately favors stable execution over
reopening a nearly completed interval.

## Required charge

The same slot-commitment rule applies to paid required charge:

- required charge is published only with an explicit grid commitment;
- actual charge reduces the remaining requirement;
- a rolling replan may lower or cancel it in the active slot;
- a rolling replan may not increase or newly start it before the next boundary;
- PV-only planned charge never becomes required grid charge.

Live PV-follow is not a paid economic commitment and remains responsive to
actual export according to operator policy.

## Discharge modes

The latched budget is consumed only in `price_sensitive` mode.

- `price_sensitive`: live discharge requires remaining optimizer budget;
- `load_following`: live control serves eligible load without consulting the
  economic budget;
- `off`: automatic discharge is blocked.

Changing modes does not erase measured slot throughput. Returning to
`price_sensitive` uses the latched or subsequently lowered commitment minus all
discharge already measured in that slot.

## State and restart behavior

Compilation state is explicit, not a hidden module global. It identifies the
active slot, original committed plan and the accepted required-charge and
discharge ceilings. A restart may restore a directive only from a valid current
plan and real progress inputs; missing or stale SoC fails closed rather than
inventing a nominal value.

The Home Assistant adapter persists one compact, versioned snapshot containing
the active compilation state, measured slot throughput and the corresponding
monotonic battery energy counters. On a clean config-entry reload, the same-slot
commitment and progress resume directly. After an unclean restart, progress is
advanced by valid monotonic counter deltas. Counter rollback, missing counters
or malformed state makes commercial grid charge and discharge fail closed until
the next slot boundary; actual PV export may still be captured because PV-follow
does not create paid economic permission. A running process always resets
progress normally when it observes the next slot itself.

Persistence is an application adapter and is not part of the pure compiler. The
compiler continues to receive only `BatteryPlan`, `SlotProgress`, explicit
`PlanCompilationState` and an issue timestamp.

`compiler_runtime.py` is the internal execution module around that pure
compiler. It owns active-slot identity, measured throughput, commitment state,
and restart restoration. The Home Assistant
coordinator supplies measurements and persists snapshots but does not recreate
those rules. The runtime does not read prices, forecasts, Home Assistant state
or battery hardware.

## Setup independence

The compiler consumes only typed plans, progress and explicit state. It must not
refer to entity IDs, device identifiers, addresses, hostnames, installation
URLs or local storage paths. Vendor and Home Assistant concerns belong to
adapters and actuation, not compiler semantics.

## Verification

Required regression scenarios include:

- current-slot budget cannot rise after reoptimization;
- current-slot budget may fall;
- the next slot accepts a higher latest budget;
- charged and discharged actuals decrement only their own permissions;
- PV-only charge never opens grid charge;
- load-following discharge does not depend on economic budget;
- mode changes preserve measured progress;
- clean reload preserves same-slot commitments and measured progress;
- unclean restart reconstructs progress from monotonic counters;
- unreconstructable same-slot progress fails commercially closed without
  disabling PV-follow;
- stale SoC and invalid slot identity fail closed.

The production migration and rollback gate is maintained in
`docs/impact-analyses/2026-08-31-phase-6-plan-compiler.md`.

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

At the start of each UTC-aligned 15-minute slot, the compiler takes the latest
economic directive for that slot. If its plan was generated before the slot
boundary, the discharge commitment is `provisional`: the first eligible plan
generated at or after the boundary may replace it once. The commitment then becomes
`final`. This corrects the pre-boundary SoC projection without allowing rolling
optimizer runs to make discharge permission jump repeatedly inside the slot.

For the active slot:

- actual charged energy reduces remaining required charge;
- actual discharged energy reduces remaining commercial discharge budget;
- the first eligible post-boundary plan may replace a provisional commitment;
- that replacement may increase only when the SoC source is currently available;
- after reload/recovery, a cached plan predating live SoC waits for the forced run;
- after reconciliation, later plans may lower or withdraw discharge permission;
- after reconciliation, later plans may not increase or newly open it;
- physical SoC and power limits may always reduce executable permission;
- increases and new economic choices apply from the next slot.

At the next slot boundary, the latest valid plan is latched without carrying the
previous slot's economic ceiling forward.

### Example

At 19:59, the plan gives the future 20:00 slot `0.20 kWh` discharge permission.
The compiler takes that value provisionally at 20:00. By the time the first
post-boundary plan completes, `0.05 kWh` has already been discharged and the
new plan gives the slot a total budget of `0.40 kWh`.

- the one-time reconciliation leaves `0.35 kWh` remaining;
- a later rolling replan proposes `0.60 kWh`: remaining stays `0.35 kWh`;
- a later rolling replan proposes `0.20 kWh`: remaining becomes `0.15 kWh`;
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
plan and real progress inputs; a missing SoC that exceeds its bounded adapter
bridge fails closed rather than inventing a nominal value. An unchanged but
available SoC remains valid for event-driven sources.

The Home Assistant adapter persists one compact, versioned snapshot containing
the active compilation state, measured slot throughput and the corresponding
monotonic battery energy counters. On a clean config-entry reload, the same-slot
commitment and progress resume directly. After an unclean restart, progress is
advanced by valid monotonic counter deltas. Counter rollback, missing counters
or malformed state makes commercial grid charge and discharge fail closed until
the next slot boundary; actual PV export may still be captured because PV-follow
does not create paid economic permission. A running process always resets
progress normally when it observes the next slot itself.

The provisional/final phase is part of the persisted compilation state. After
a reload or SoC recovery, a cached post-boundary plan that predates the live SoC
source leaves the commitment unchanged and provisional until the acknowledged
forced planner run completes. With an unavailable/bridged source, the first
post-boundary plan may only lower the provisional value and still makes the
state `final`; a later plan cannot reopen the slot. A snapshot from a version
without that field is migrated as `final`, which cannot reopen permission.
Clean reload and counter-reconstructable crash recovery retain the phase.

If no usable snapshot exists at all when a deployment starts inside an active
slot, the runtime cannot distinguish prior execution from downtime. It therefore
opens only the fraction of the optimizer's discharge budget corresponding to
the unelapsed slot time. That proportional ceiling is latched, cannot rise
during later replans and is immediately `final`. Required grid charge remains
closed. An exact clean snapshot or progress reconstructed from monotonic
counters always takes precedence over this fallback.

The coordinator forces one optimizer refresh at or after each slot boundary.
There is no separate last-minute prefetch run: it previously modeled the nearly
elapsed current slot as a complete interval and could distort the next slot's
SoC. A boundary force is acknowledged only after the planner accepts or queues
it, so input-capture failure cannot consume the refresh.

A temporary missing or incomplete planner result fails commercially closed for
that refresh but does not erase an already established active-slot commitment.
The same preservation applies to an invalid or causally impossible plan.
When a valid plan returns for the same slot, compilation resumes from the
latched ceiling and measured progress. This prevents an asynchronous optimizer
refresh from reopening permission after a restart or replan.

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
- missing same-slot progress prorates discharge to the unelapsed slot fraction
  while keeping grid charge closed;
- a temporary same-slot plan gap remains closed and cannot erase or reopen the
  latched commitment;
- unavailable SoC beyond its bounded bridge and invalid slot identity fail
  closed.

The approved semantics and their rollback evidence are recorded in
`docs/impact-analyses/2026-08-31-phase-6-plan-compiler.md`.

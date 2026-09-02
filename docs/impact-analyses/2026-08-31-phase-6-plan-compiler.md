# Impact analysis: Phase-6 plan compiler extraction

## Reason and scope

Phase 5 makes economic optimization available through the pure
`optimize(problem) -> BatteryPlan` boundary. The production path still combines
translation of the current plan slot, slot-local progress accounting and
coordinator-owned state. Phase 6 moves only the deterministic translation behind
the approved state-explicit `PlanCompiler.compile(...)` contract.

This preparation now includes the deployed Phase-5 pure-optimizer cutover. It
does not change the coordinator's plan compilation, live controller or actuator
and is not itself a deployment candidate.

## Contract interpretation

The initial preparation identified two incomplete in-memory contracts. The
owner approved their behavioral semantics on 2026-08-31. The approved slot
commitment is documented in `docs/plan-compiler/README.md`; operator and EV
policy precedence is documented in `docs/live-control/README.md`.

The base compiler rules remain:

- the compiler selects exactly the `BatteryPlanSlot` named by `SlotProgress`;
- measured charge reduces remaining required charge;
- measured discharge reduces the optimizer's commercial discharge budget;
- measured SoC may only reduce discharge permission to physically available
  energy above the minimum SoC;
- grid charging is authorized only by an explicit planned grid commitment and
  non-zero required charge in the same slot;
- PV permission and all physical power limits come from the plan and its copied
  battery constraints;
- the compiler reads no prices, forecasts, Home Assistant state, configuration,
  history, wall clock, files or network resources.

It never moves energy between slots, increases commercial permission, infers a
charge source or modifies the optimizer's future SoC trajectory.

## Preparation and production cutover

The first change introduced a side-effect-free compiler and executable boundary
tests. The prepared shadow release now:

1. capture the same immutable `BatteryPlan` and slot-progress snapshot for the
   old and new compiler;
2. compare directives outside Home Assistant Recorder;
3. prove parity for required charge, source permissions, discharge budget, SoC
   limits and slot validity;
4. keeps the established directive solely authoritative;
5. writes a bounded comparison trace and exposes a diagnostic status sensor.

The separately prepared cutover commit makes the pure compiler authoritative
only after explicit owner approval and the observation gate. It retains the old
translation for one comparison window; Phase 7 removes that transitional path.

## Gate and rollback

- Full contract, optimizer and integration regressions pass.
- The compiler module has no HA runtime or I/O dependency.
- Phase-5 cutover is complete; its stabilization window continues before the
  compiler becomes authoritative.
- Shadow directives match the production path across grid charge, PV-only
  charge, price-sensitive discharge, load discharge, EV activity, manual mode,
  SoC limits, restart recovery and within-slot re-optimization.
- Live command and actuator traces remain unchanged before compiler cutover.

Rollback before cutover is branch deletion. Rollback after cutover returns to
the last Phase-5 release; no permanent runtime selector is introduced.

## Approved contract extensions

The pure compiler cannot preserve every production control without leaving
hidden state or policy in the coordinator. Two approved extensions are required
before a production shadow can be wired:

### Explicit compilation state

Price-sensitive operation currently accepts a lower budget from a re-optimized
plan inside the active slot but does not accept a later increase. This prevents
rolling replans from repeatedly reopening energy already withheld or consumed.
`SlotProgress` contains measured energy only, so a stateless compiler cannot
distinguish a newly increased authorization from the original slot budget.

Approved direction: add an immutable `PlanCompilationState` containing the
active slot and the previously authorized base budget. Compilation receives and
returns that state explicitly. A slot change resets it; within a slot the base
may decrease but not increase. This is an in-memory contract change with no
persisted schema or dashboard migration.

### Explicit operator policy in live control

`LivePolicy` already owns EV behavior but does not represent the configured
discharge mode or manual charge/discharge override. Production currently
implements these before or around plan compilation:

- `Preissensitiv` consumes only optimizer budget;
- `Bei Last` may follow eligible household load without economic budget;
- `Aus` blocks automatic discharge;
- manual charge/discharge temporarily overrides automatic plan execution.

Approved direction: add typed discharge mode and manual override fields to
`LivePolicy`. The directive continues to carry physical power and SoC limits;
zero commercial budget blocks only price-sensitive discharge. The live
controller applies the explicit operator choice without reading HA options or
reinterpreting prices. `strategy_enabled` remains orchestration around the
single actuator and is not moved into the pure live function.

Both extensions preserve existing user-visible behavior and remove policy from
the coordinator. They do not authorize a second actuator path. Their contract
types and preparation tests are implemented on this branch; production
migration remains subject to the Phase-5 and Phase-6 gates.

## Status

- Proposed and locally prepared: 2026-08-31
- Contract approval: approved by owner on 2026-08-31
- Contract implementation: prepared locally
- Production shadow integration: implemented locally
- Authoritative cutover: prepared separately, not deployed
- Deployment: ready for the 12-24 hour Phase-6 shadow gate

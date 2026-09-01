# Impact analysis: Phase-5 pure optimizer extraction

## Reason and scope

Phase 4 made one immutable `ForecastBundle` the sole production forecast input.
The economic dynamic program, canonical charge scheduling and commercial
discharge-budget calculation still live inside `optimizer_engine.py`, where
module globals also own Home Assistant, Recorder, persistence and reporting
concerns. Phase 5 extracts those calculations behind the existing pure
`optimize(problem) -> BatteryPlan` boundary.

This branch prepares a shadow-only deployment. The existing optimizer remains
authoritative and continues to supply the plan, compiler and live controller.
The pure optimizer receives the same immutable forecast, market, SoC,
constraints and derived commercial policy once per 15-minute slot. Its result
is compared and retained, but cannot reach the actuator. The current
`0.2.0-rc.1` remains the rollback point.

## Proposed contract extension

The reviewed `OptimizationProblem` already carries the aligned market,
forecast, battery state and physical constraints. `CommercialPolicy` does not
yet carry all policy values that currently enter the optimizer through module
globals. The following additive fields are proposed:

- export opportunity value;
- optional discharge feasibility floor;
- independent PV charge, grid charge and discharge permissions;
- PV-recovery confidence and uncertainty reserve.

All energy remains kWh per slot, power remains W, prices remain ct/kWh and SoC
remains percent. Defaults preserve the current contract examples: zero export
value, all directions enabled, 75% PV confidence and 0.30 kWh reserve. The
fields are in-memory only and require no persisted schema migration.

These values belong to commercial optimization, not forecasting, plan
compilation or live control. Making them explicit removes hidden runtime input;
it does not authorize the live controller to exceed `BatteryPlan` permissions.

## Dependency and behavior impact

The pure optimizer may read only `OptimizationProblem` and immutable algorithm
constants identified by its `optimizer_version`. It performs no Home Assistant,
database, network, file or clock I/O. Price-history and EEX enrichment remain in
the problem-building adapter and must be reduced to explicit terminal/floor
policy before optimization.

The first cutover must preserve the current dynamic-programming objective,
round-trip transitions, deterministic charge tie-breaking, micro-cycle
suppression, sub-quantum grid-charge deferral, PV-headroom recovery and
commercial discharge budgets exactly. Forecasting and live meter following are
out of scope.

The temporary shadow trace is stored outside Home Assistant Recorder as bounded
JSONL with 14-day/1,500-record retention. Only compact maximum deltas, mismatch
count and total cost deltas are retained. Shadow evaluation and persistence
errors are contained and cannot fail the authoritative optimizer run.

## Verification and gate

- Golden-master parity for every retained historical problem that can be
  reconstructed from the current feature store and price history.
- Explicit tests for RTE, terminal value, horizon boundaries, PV recovery,
  grid/PV source permissions, EV-free load and later higher-price reservation.
- Identical `BatteryPlan` slot actions, SoC, budgets and costs within documented
  rounding tolerance.
- Full pytest, HACS and Hassfest validation before any merge or deployment.

## Rollout and rollback

The pure implementation is prepared on `codex/phase-5-pure-optimizer`. It is
not wired into production until Phase 4 has passed its live observation gate,
the proposed contract semantics are explicitly approved and retained-history
parity passes. Cutover uses one optimizer path with Git rollback; no permanent
old/new runtime selector is introduced.

## Status

- Proposed: 2026-08-30
- Contract approval: approved by owner on 2026-08-30
- Local implementation: shadow candidate ready
- Production cutover: prepared after 242 retained runs passed operational
  parity on 2026-09-01; deployment remains a separate controlled step

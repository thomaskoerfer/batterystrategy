# Impact analysis: Phase-7 transitional cleanup preparation

## Decision

Phase 7 is the final phase of the documented architecture transformation. This
stacked local branch prepares that cleanup, but it is deliberately not a
deployment candidate. The Phase-6 pure compiler has not yet completed its live
shadow and command-trace gate, so deleting the coordinator-owned compiler would
violate the migration order and remove the proven rollback point.

## Removed transitional paths

- The old dynamic-programming kernel is deleted. Production and regression
  scenarios now exercise the single authoritative pure optimizer.
- The completed forecast and optimizer shadow runners, stores, diagnostics and
  tests are deleted. Their one-time optimizer trace is removed during upgrade.
- Commercial horizon metadata is computed without running a second plan.
- Market context, one-shot planning orchestration and measured savings are
  extracted into three cohesive components rather than speculative
  micro-services. `optimizer_engine.py` delegates through compatibility
  facades while existing callers migrate.
- Direct SQLAlchemy and Recorder-table access is deleted. Bounded numeric
  history is captured through Home Assistant's public history API by the data
  adapter, while calibration bootstrap uses canonical finalized feature slots.

No interface contract changes. Units, signs, slot alignment, plan semantics,
live commands and actuator targets are intended to remain unchanged.

## Dependency and decision impact

The data adapter now owns Recorder access and maps configured entity roles to
normalized numeric series before orchestration consumes them. Forecasting and
optimization do not receive Home Assistant or database objects. The history
window is 49 hours, sufficient for the existing two-local-day savings repair
and 48-hour dashboard profiles. Longer learning history comes from the bounded
feature store instead of raw Recorder states.

Removing the old optimizer is behavior-preserving because the pure optimizer
is already authoritative and its retained operational parity gate passed. The
deleted private-kernel tests are replaced by public optimizer scenarios and
Phase-7 guards that prohibit a second kernel, shadow module or direct schema
dependency from returning.

## Chief-architect review

The target architecture is coherent: immutable normalized facts feed separate
load/PV forecasting, one pure optimizer, a deterministic slot compiler, fast
live policy and one actuator boundary. Contracts prohibit the important failure
modes: hidden I/O in forecasting/optimization, economics in live control and
multiple hardware writers.

The implementation is not yet at that target. Three high-priority blockers
remain before Phase 7 can be called complete:

1. The pure Phase-6 compiler is not authoritative. `strategy.py` and
   `coordinator.py` still translate plan fields and latch slot progress.
2. Home Assistant service writes remain in `coordinator.py`; `actuator.py`
   currently computes targets and write decisions but is not the sole writer.
3. `optimizer_engine.py` still combines forecast composition, output assembly
   and mutable runtime orchestration. Market enrichment, planning/publication
   and savings accounting are now delegated to dedicated coarse components,
   but the compatibility facade is not the permanent application entry point.

These are structural findings, not reasons to change working live control in
this cleanup branch. Phase-6 parity must be observed first; actuation relocation
then requires service-call parity tests; orchestration extraction can proceed
behind unchanged contracts without changing decisions.

## Verification and gate

Local preparation requires compilation, all unit/contract tests, historical
optimizer scenarios, architecture-documentation checks and explicit absence of
the removed modules and SQL tokens. Before deployment it additionally requires:

- Phase-6 directive parity across price-sensitive, load, EV, manual, restart,
  charge-source and within-slot replan cases;
- a non-production Recorder-history timing and savings-parity check;
- HACS and Hassfest validation;
- explicit owner approval for cutover;
- several days of live health and command-trace review before deleting the
  remaining coordinator compiler and moving service writes.

Rollback before deployment is branch deletion. A later deployment must be
based on a tagged Phase-6 release and retain its documented server rollback.

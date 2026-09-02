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

The stacked ready branch closes the high-priority structural findings without
changing the approved contracts:

1. The pure Phase-6 compiler is the sole directive authority. Coordinator
   shadow state and the previous slot latch are removed.
2. Home Assistant battery service calls are owned exclusively by
   `HomeAssistantZendureActuator`; the coordinator only orchestrates safety and
   enable/disable transitions.
3. Forecasting, market context, optimization, planning and savings are
   delegated to documented coarse components. `optimizer_engine.py` remains a
   deliberately thin runtime compatibility facade rather than being split into
   installation-specific micro-layers.

Static architecture tests prevent the old compiler path, shadow modules,
direct database access and coordinator hardware writes from returning.

## Verification and gate

Local preparation requires compilation, all unit/contract tests, historical
optimizer scenarios, architecture-documentation checks and explicit absence of
the removed modules and SQL tokens. Before deployment it additionally requires:

- Phase-6 directive parity across price-sensitive, load, EV, manual, restart,
  charge-source and within-slot replan cases;
- a non-production Recorder-history timing and savings-parity check;
- HACS and Hassfest validation;
- explicit owner approval for cutover;
- a short live health and command-trace review after each separately approved
  deployment stage.

Rollback before deployment is branch deletion. A later deployment must be
based on a tagged Phase-6 release and retain its documented server rollback.

# Impact analysis: Phase-7 final architecture cleanup

## Decision

Phase 7 is the final phase of the documented architecture transformation. The
Phase-6 pure compiler completed its slot-boundary parity and command-trace gate
before this candidate was rebuilt on the tagged Phase-6 release. The completed
comparison implementations can therefore be removed without changing the
authoritative optimizer, compiler or live behavior.

## Removed transitional paths

- The old dynamic-programming kernel is deleted. Production and regression
  scenarios now exercise the single authoritative pure optimizer.
- The completed forecast, optimizer and compiler shadow runners, stores,
  diagnostics, comparison contracts and tests are deleted. Their bounded runtime files are removed
  during upgrade after the authoritative learned state is preserved.
- Commercial horizon metadata is computed without running a second plan.
- Market context, one-shot planning orchestration and measured savings are
  extracted into three cohesive components rather than speculative
  micro-services. The former compatibility engine is removed; the HA adapter
  invokes the explicit planning pipeline and receives a typed Python result.
- Direct SQLAlchemy and Recorder-table access is deleted. Bounded numeric
  history is captured through Home Assistant's public history API by the data
  adapter, while calibration bootstrap uses canonical finalized feature slots.

The owner explicitly approved removal of the unused shadow-evaluation contract
on 2026-09-03 as part of the complete legacy cleanup. No active producer or
consumer used it. Units, signs, slot alignment, plan semantics, live commands
and actuator targets remain unchanged.

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

The final candidate closes the high-priority structural findings without
changing the approved contracts:

1. The pure Phase-6 compiler is the sole directive authority. Coordinator
   shadow state and the previous slot latch are removed.
2. Home Assistant battery service calls are owned exclusively by
   `HomeAssistantZendureActuator`; the coordinator only orchestrates safety and
   enable/disable transitions.
3. Forecasting, market context, optimization, planning and savings are owned by
   documented coarse components. The planning pipeline only sequences them and
   publishes the established entity payload; it is not a compatibility facade
   and owns no duplicate domain implementation.

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

The candidate is based on tagged Phase 6 and retains the documented server
rollback. Deployment still requires explicit owner approval and the normal
post-restart health and command-trace review.

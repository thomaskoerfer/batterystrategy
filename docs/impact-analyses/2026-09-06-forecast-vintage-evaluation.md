# Forecast Vintage Evaluation

Status: Implemented locally, pending observation

## Purpose

The existing perfect-foresight backtest evaluates battery operation but cannot
reconstruct which forecast vintage produced an earlier plan. Existing online
forecast evaluation measures only a coarse one-hour aggregate. A bounded record
of immutable forecast outputs is therefore required to compare load, PV and
named load components by lead time without reconstructing historical knowledge.

## Contract impact

No executable interface contract changes. `ForecastBundle`, forecast-slot,
quality and feature-store semantics remain unchanged. `PlanningRunOutcome`
exposes the already-produced bundle to the Home Assistant persistence adapter as
an internal observational value; it is not an optimization, compiler or live
control input.

The sidecar uses evaluation schema 1. It is deliberately not added to the
binding domain contracts because no production decision consumes it. A future
feedback, retraining or model-promotion mechanism would change behavior and
requires a separate impact analysis and owner approval.

## Runtime and storage impact

The Home Assistant adapter schedules one compressed vintage at most once per
UTC quarter-hour after authoritative state persistence and result caching. The
write runs in an independent best-effort executor task, so slow storage cannot
hold the planner lifecycle open. Trace retention is 21 days, each forecast is
capped at the maximum supported 192 slots and 32 components, and no values are
added to Recorder attributes. Concurrent writers publish only complete files
and preserve the first vintage. Write or retention failure is rate-limited in
the log and cannot replace the cached plan or reach the actuator.
Trace persistence is single-flight; if storage is slow, later vintages are
discarded rather than accumulating work in Home Assistant's shared executor.
A non-blocking OS file lock spans config-entry reloads and is automatically
released if the process exits, while each asynchronous task remains owned and
cancelled by the entry that created it. Revoked adapters cannot schedule
replacement work after unload.

The trace contains normalized model outputs only. It contains no entity IDs,
hostnames, URLs, device identifiers, credentials or provider payloads.

## Evaluation semantics

The offline evaluator uses exact UTC slot alignment. It scores only targets
whose complete slot follows the forecast generation time and has ended by the
explicit evaluation cutoff. It excludes insufficient coverage and relevant
missing, estimated, reset or restart evidence. Whole-house load uses the
canonical EV-free actual; EV charging is never added back.

Load and PV retain their own generation timestamps because the executable
contract requires an aligned target grid but does not require simultaneous
model generation. Unknown trace or feature-store schemas are rejected rather
than mixed silently.

Metrics are grouped by series, model version and lead-time bucket. At least
seven complete days are required before weekday-sensitive comparisons. There is
no automatic winner or runtime feedback path; any model change must declare
series-specific expectations and inspect regressions rather than relying on one
aggregate score.

## Verification and rollback

Tests cover serialization, model metadata, quantiles, components, one-vintage
frequency, retention, maturation, exact alignment, missing-data exclusion, EV
exclusion, lead-time grouping, architecture isolation and write-failure
containment.

Rollback removes the trace hook and evaluator. Existing compressed trace files
may be deleted or left to age out; they are non-authoritative and require no
planning, compiler or actuator state migration.

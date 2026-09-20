# Evaluation and diagnostics

## Purpose

Evaluation measures forecast quality, optimizer parity and strategy value
without participating in battery decisions. Diagnostics explain data freshness,
model versions, readiness and failures without exposing private configuration.

## Non-authoritative boundary

Evaluation consumes immutable outputs and later matured actual slots. It may
write bounded reports, forecast observations, backtest results and command
traces. It cannot feed a live command, change a plan, retrain during a backtest
window or obtain an actuator reference. No duplicate authoritative forecast,
optimizer or compiler path remains at runtime. The temporary owner-approved
stochastic optimizer shadow is the sole exception: it runs only after
authoritative publication, has no compiler/actuator reference and is removed or
promoted after its release gate.

## Metrics

Forecast evaluation treats load and PV independently and reports coverage, MAE,
bias, daily energy and lead-time buckets. Optimizer comparison reports action,
SoC, budget and cost deltas. Perfect-foresight replay is a diagnostic benchmark,
not a realizable live strategy.

Savings metrics must distinguish predicted value from measured battery flows.
Their units, price source and PV/export valuation must be explicit.

## Persistence and privacy

Evaluation stores compact bounded traces outside Home Assistant Recorder where
large attributes would harm recorder performance. Retention and file growth are
bounded. `command_trace.py` owns command-trace serialization and retention; the
live coordinator only schedules that observational write in the executor.
Public diagnostics redact configured entities, locations, credentials, device
identifiers and provider payloads.

`forecast_trace.py` records at most one immutable forecast vintage per UTC
quarter-hour in a compressed, 21-day sidecar with an additional 64 MiB cap
below the Home Assistant config
directory. Each vintage contains the already-produced load and PV contract
outputs, model versions, target slots, P50 and optional calibrated quantiles,
quality metadata and named load-component forecasts. Schema 4 also stores the
separate EV marginal forecast, bounded coherent scenario set, Scenario Builder
diagnostics and authoritative and stochastic-shadow plans,
so identical vintages can be compared against actuals and perfect foresight.
The same sidecar stores the redacted normalized market curve, battery state and
constraints, commercial policy and EV interaction policy required to reproduce
the optimization problem; it contains no entity or device identifiers.
It contains no entity IDs, provider payloads or actuator data. A trace write happens
only after the authoritative planning result has been persisted and cached, in
an independent best-effort executor task. Failure is rate-limited in the log and
cannot invalidate or delay planner completion. Concurrent reload-era writers
publish atomically and preserve the first complete vintage for a quarter-hour.
Only one trace write may occupy the shared executor at a time; an overdue write
causes later observational vintages to be skipped rather than queued. The
non-blocking filesystem lock is acquired before scenario generation and spans
config-entry reloads, background tasks belong to the active entry lifecycle,
and a revoked adapter cannot enqueue new work.

The offline `scripts/battery_strategy_forecast_backtest.py` utility joins those
vintages to finalized feature-store actuals by exact UTC slot key. It excludes
the vintage's current partial slot, targets that have not ended, insufficiently
covered actuals and relevant missing/reset/restart flags. It reports sample
count, MAE, signed bias, RMSE, WAPE and energy totals separately for whole-house
load without EV, PV and each named load component, grouped by model version and
lead-time bucket. Example:

```sh
python3 scripts/battery_strategy_forecast_backtest.py \
  --trace-dir PATH_TO_FORECAST_TRACE \
  --feature-store PATH_TO_FEATURE_STORE \
  --days 7
```

These metrics are evidence for a later reviewed model change, not an online
reinforcement loop. The existing online calibration remains owned by the
forecast application and is unchanged by this trace or evaluator.

The replacement optimizer shadow has a separate evaluator. It reports load/PV
scenario CRPS and central-80% coverage, EV-event Brier score, shadow runtime and
first-policy monetary regret versus a hindsight-perfect replay. Only the first
HA capture within 30 seconds after a slot boundary enters the decision
comparison; later mid-slot replans are excluded. Compiler progress accounting
starts from the captured SoC, while perfect-foresight load/PV/EV actuals in the
first slot are scaled to its remaining fraction after scheduler latency.
Complete matured path suffixes additionally report an energy score, a local
variogram score and EV start/duration error, so marginal calibration cannot hide
implausible temporal or cross-series paths. The replay uses the first eligible
boundary vintage in each UTC hour to keep runtime bounded and prevent post-hoc
sample selection. Realized live cost and
export remain owned by measured savings/command evaluation; a planning trace
cannot reconstruct compiler and live-control intervention faithfully:

```sh
python3 scripts/battery_strategy_optimizer_shadow_backtest.py \
  --trace-dir PATH_TO_FORECAST_TRACE \
  --feature-store PATH_TO_FEATURE_STORE \
  --days 7
```

This report replaces the former RC26 observation gate. RC26 is retained only as
a rollback reference and is not executed as a second shadow.

Finite cyclic appliances also have an event-level walk-forward evaluator. It
uses only prior completed cycles, starts each replay after the first finalized
active slot and reports remaining-energy, slot-shape and end-slot errors:

```sh
python3 scripts/battery_strategy_appliance_walkforward.py \
  PATH_TO_FEATURE_STORE --component-key appliance_key
```

The report keeps the power-only forecast separate from a historical-context
proxy based only on quality-valid features stored in that first active slot.
This makes optional activity, progress and expected-end context visible without
claiming that slot-average history is an exact reconstruction of a live state.

## Setup independence

Metric definitions use contract fields and model identifiers, not household
names, addresses, hostnames, URLs, serial numbers or entity IDs. A report may
describe a supported device/provider class but must remain reproducible for any
installation supplying equivalent normalized inputs.

## Verification

Tests cover alignment, actual-data maturation, missing-data exclusion,
non-authoritative flags, retention, bounded attributes, redaction and failure
containment. A release gate must state its observation duration and numerical
tolerances before results are reviewed.

The replacement-shadow gate is pre-registered as follows: at least seven UTC
days with at least 90% of quarter-hour vintages, 100 first-boundary-cycle
decision vintages, 20 complete
matured path vintages and 30 samples in every observed lead/regime cohort;
maximum shadow runtime 5 seconds; the upper 95% confidence bound from a paired
UTC-day block bootstrap of monetary-regret delta must not exceed zero; and each
Scenario Builder completion must cover at least 90% of eligible sampled
vintages; each
load/PV cohort must have central-80% coverage between 65% and 95% with scenario
CRPS no worse than the P50 degenerate baseline. Joint energy and variogram
scores must be no worse than the degenerate P50 path, and EV Brier/start/duration
scores must be no worse than the climatology/inactive-EV baselines. At least
three distinct matured EV sessions and 30 inactive EV slots are required before
the EV gate is eligible. EV timing uses probability-weighted absolute path
error rather than error of the mean event time. Non-finite policy costs fail.
The evaluator returns `pass`, `fail` or
`insufficient_data`; it never promotes code itself.

Collect at least seven complete days before comparing weekday-sensitive model
behavior. No automatic promotion threshold is defined: a proposed forecast
change must state its expected series and lead-time improvements and must not
hide regressions behind one aggregate score.

## Production status

Durable evaluation means explicit backtests, matured forecast metrics and
bounded command traces, not a dormant second implementation.

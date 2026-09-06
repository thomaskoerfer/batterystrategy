# Evaluation and diagnostics

## Purpose

Evaluation measures forecast quality, optimizer parity and strategy value
without participating in battery decisions. Diagnostics explain data freshness,
model versions, readiness and failures without exposing private configuration.

## Non-authoritative boundary

Evaluation consumes immutable outputs and later matured actual slots. It may
write bounded reports, forecast observations, backtest results and command
traces. It cannot feed a live command, change a plan, retrain during a backtest
window or obtain an actuator reference. No duplicate forecast, optimizer or
compiler implementation remains at runtime.

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
quarter-hour in a compressed, 21-day sidecar below the Home Assistant config
directory. Each vintage contains the already-produced load and PV contract
outputs, model versions, target slots, P50 and optional calibrated quantiles,
quality metadata and named load-component forecasts. It contains no entity IDs,
provider payloads, battery decisions or actuator data. A trace write happens
only after the authoritative planning result has been persisted and cached, in
an independent best-effort executor task. Failure is rate-limited in the log and
cannot invalidate or delay planner completion. Concurrent reload-era writers
publish atomically and preserve the first complete vintage for a quarter-hour.
Only one trace write may occupy the shared executor at a time; an overdue write
causes later observational vintages to be skipped rather than queued. The
non-blocking filesystem lock spans config-entry reloads, background tasks belong
to the active entry lifecycle, and a revoked adapter cannot enqueue new work.

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

Collect at least seven complete days before comparing weekday-sensitive model
behavior. No automatic promotion threshold is defined: a proposed forecast
change must state its expected series and lead-time improvements and must not
hide regressions behind one aggregate score.

## Production status

Durable evaluation means explicit backtests, matured forecast metrics and
bounded command traces, not a dormant second implementation.

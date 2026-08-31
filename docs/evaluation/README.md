# Evaluation and diagnostics

## Purpose

Evaluation measures forecast quality, optimizer parity and strategy value
without participating in battery decisions. Diagnostics explain data freshness,
model versions, readiness and failures without exposing private configuration.

## Non-authoritative boundary

Evaluation consumes immutable outputs and later matured actual slots. It may
write bounded comparison traces, reports and backtest results. It cannot feed a
live command, change a plan, retrain during a backtest window or obtain an
actuator reference.

Shadow implementations always set `authoritative = false`. A shadow error is
contained and cannot fail the production optimizer or live controller.

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
bounded. Public diagnostics redact configured entities, locations, credentials,
device identifiers and provider payloads.

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

## Migration status

Forecast and optimizer shadow traces are available during their respective
migration phases. Transitional traces are removed after cutover and rollback
windows unless they provide durable operational value with documented bounded
retention.

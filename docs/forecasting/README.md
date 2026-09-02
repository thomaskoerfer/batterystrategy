# Forecasting

## Purpose

Forecasting predicts future EV-free household load and PV generation on one
shared quarter-hour grid. It is deterministic and side-effect free once its
history, weather, current context and request have been assembled.

## Independent models

`LoadForecaster` and `PvForecaster` are independent boundaries:

- load forecasting may use historical household load, named load components,
  time/calendar features, weather and current device context;
- PV forecasting may use historical PV generation, plant limits, time/solar
  shape, weather and learned PV bias;
- neither model may read the other's configuration, features or learned state;
- net load is derived from load minus PV and is not a third learned forecast.

Changing PV behavior must not change load output. Adding or changing a heat
pump, air conditioner or generic load component must not alter PV behavior or
unrelated load components.

## Contract

Both forecasters receive a `ForecastRequest` and return aligned immutable slot
tuples. P50 is mandatory. P10/P90 are emitted only as a calibrated pair with
sufficient matured residuals. Every forecast identifies its model version,
training cutoff and quality.

The combined `ForecastBundle` is constructed before optimization. Forecasting
does not know prices, battery SoC, battery constraints, terminal value or a
battery plan.

## Load components

The whole-house target always excludes EV charging. Independently metered loads
may be forecast separately and composed with a residual general-house component.
Each component owns its features, warm-up gate, model version and quality.
Missing component data remains in total household load and is never silently
treated as zero or subtracted twice.

Current profiles support heat-pump domestic-hot-water and space-heating context,
a shared-meter multi-zone air-conditioning context and a generic metered load.
Additional profiles must use stable semantic feature keys rather than entity
names or vendor payload fields.

## PV model

PV output is constrained by configured plant and inverter capability and may use
weather and learned slot bias. Historical plant changes belong to explicit
backtest preparation; operational forecasting must not accumulate permanent
one-household correction branches.

## Setup independence

Models consume normalized contracts, never entity IDs, addresses, device serial
numbers, hostnames or installation URLs. Supported provider/device classes may
be documented, but model semantics must remain portable to another installation
with equivalent normalized inputs.

## Verification

Load and PV are evaluated separately by lead time, time of day, MAE, bias, daily
energy and quality coverage. Tests prohibit prices, SoC and optimizer imports;
check component summation and missing-data behavior; and prove that load and PV
changes cannot affect each other unintentionally.

## Migration status

Feature-store forecasting is authoritative. The slot-profile helpers are its
single parity-preserving mathematical source, not a second runtime selector.
Completed shadow composition and rollback-only paths are removed in Phase 7.

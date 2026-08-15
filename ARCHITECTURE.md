# Battery Strategy Architecture

This document defines the target architecture and the migration rules for
Battery Strategy. Contributors should read it before changing forecasting,
optimization, history access, plan directives, or live battery control.

## Safety invariant

There is exactly one battery actuation path. Shadow implementations may produce
diagnostics and comparison results, but they must never write battery mode or
power limits. Only the live controller may call the actuator.

## Target data flow

```text
Home Assistant entities, recorder, weather and market data
                              |
                              v
                 Data adapters and feature store
                              |
                              v
                         Forecasting
                              |
                              v
                         Optimization
                              |
                              v
                         Plan compiler
                              |
                              v
                        Live controller
                              |
                              v
                           Actuator
```

Evaluation, diagnostics and backtesting observe the typed outputs of each layer.
They do not participate in live actuation.

## Layer responsibilities

### Data adapters and feature store

- Read configured Home Assistant entities and external market/weather inputs.
- Normalize units, timestamps, signs and data-quality flags.
- Reconstruct measured house load without EV and measured PV generation.
- Aggregate irregular live samples into time-weighted 15-minute features.
- Persist compact, versioned features independently of the recorder backend.
- Use Home Assistant recorder APIs only for bootstrap, backfill and repair.
- Make no forecast or battery decision.

The optimizer and forecasters must not receive a SQLAlchemy engine, database URL,
Home Assistant object, entity ID, or storage path.

### Forecasting

`LoadForecaster` predicts house load without EV. `PvForecaster` predicts PV
generation and owns PV-capacity normalization, weather adjustment and learned
slot bias. Both return point estimates, uncertainty and data-quality metadata in
a shared `ForecastBundle`.

Forecasting does not know electricity prices, battery SoC, battery limits or a
planned battery schedule. Net load is derived from load minus PV; it is not a
third independently learned forecast.

### Optimization

Optimization is a deterministic, side-effect-free function of:

- time grid and market prices;
- `ForecastBundle` or forecast scenarios;
- current battery state and physical constraints;
- efficiency, feed-in value and commercial policy.

It produces a `BatteryPlan` containing the intended energy trajectory, charge
and discharge actions, commercial discharge budgets and plan diagnostics. It
does not read Home Assistant, history, weather, files, network resources or the
wall clock.

### Plan compiler

The plan compiler converts the economic `BatteryPlan` into explicit slot-bound
`PlanLiveDirective` values: PV charge permission, required charge, grid-charge
permission, discharge budget, SoC bounds and validity timestamps. The live
controller must not infer commercial intent from planned power or prices.

### Live controller

The live controller runs on the fast coordinator interval. It combines the
current directive with live grid, PV, EV, battery and SoC measurements. It owns
meter following, EV policy, manual controls, budget consumption, command
smoothing and fail-safe behavior. It does not optimize prices or retrain a
forecast.

### Actuator

The actuator is the only hardware-writing boundary. It translates a validated
live command into vendor controls and enforces write throttling and safe zeros.

## Persistence and recorder independence

The target feature store contains one quality-scored record per 15-minute slot,
not raw 10-second states. A 180-day retention window is sufficient for weekday
and seasonal learning while remaining small when compressed. Forecasts, plans
and their later actuals must be retained in compact form so forecast accuracy
and strategy value can be backtested without reconstructing what the system
would have known at the time.

The Home Assistant recorder backend may be MariaDB, PostgreSQL or SQLite. Its
choice must not affect forecasting or optimization semantics. Direct recorder
schema queries are transitional and must not spread beyond the history adapter.

## Current implementation debt

The existing live boundary is partly separated across `coordinator.py`,
`strategy.py` and `actuator.py`. The active forecast, recorder access, weather,
market enrichment, savings and optimization are still combined in
`optimizer_engine.py`, with runtime data supplied by `optimizer_adapter.py`.
That module is a migration source, not the desired permanent boundary.

## Migration plan and gates

### Phase 0: Baseline and contracts

- Freeze representative historical scenarios and current regression output.
- Introduce typed contracts for historical features, forecasts, optimization
  inputs, battery plans and evaluation results.
- Add forecast MAE/bias, plan parity and data-quality diagnostics.
- Change no production decisions.

Gate: all existing tests pass and serialized plan/live outputs are unchanged.

### Phase 1: Extract forecasting without changing mathematics

- Move active load and PV forecasting out of `optimizer_engine.py`.
- Keep coefficients, capacity normalization, weather inputs and bias updates
  identical.
- Run old and extracted forecasters in shadow mode for several days.

Gate: slot outputs are numerically identical apart from explicit rounding, and
the shadow path cannot reach the actuator.

### Phase 2: Add the feature store in parallel

- Aggregate live measurements into finalized 15-minute records.
- Persist versioned, compressed records with missing-data and coverage flags.
- Migrate existing learned samples without resetting bias state.
- Continue using recorder history as the production source.

Gate: at least seven days with complete slot coverage, bounded disk growth and
no effect on commands, forecasts or savings.

### Phase 3: Shadow recorder-independent forecasting

- Feed the extracted forecasters from the feature store in shadow mode.
- Compare history-derived and feature-store-derived forecasts and backtests.
- Repair discrepancies in aggregation, restart handling and unit conversion.

Gate: seven to fourteen days of acceptable forecast parity and no unexplained
energy imbalance.

### Phase 4: Cut forecasting over to the feature store

- Make the feature store the production forecast source.
- Retain recorder access only for bootstrap/backfill through one adapter.
- Keep the existing optimizer and live controller unchanged.

Gate: several days of stable live operation and forecast metrics before old
recorder-query code is removed.

### Phase 5: Extract a pure optimizer

- Move dynamic programming and commercial budget logic behind a pure
  `optimize(problem) -> BatteryPlan` interface.
- Inject prices, forecasts, SoC and policy explicitly.
- Remove module-global runtime context from optimization.

Gate: golden-master parity across the full retained history plus explicit edge
tests for RTE, terminal value, PV headroom, EV exclusion and horizon boundaries.

### Phase 6: Formalize the plan compiler

- Make every live permission and budget an explicit plan output.
- Remove any remaining commercial re-interpretation from the live controller.
- Preserve the proven meter-following and safety implementation.

Gate: plan/directive/live contract tests and several days of command-trace review.

### Phase 7: Remove transitional code

- Delete direct recorder-schema access and obsolete optimizer globals.
- Keep one production forecast path, one optimizer path and one actuator path.
- Update diagnostics, documentation, release notes and migration tests.

Gate: HACS, Hassfest, unit tests, historical backtests and live health checks pass.

## Refactoring rule

Each phase is released and observed before the next cutover. Refactoring must
preserve behavior first; forecast or optimization improvements are separate,
measurable changes after the corresponding boundary is stable.

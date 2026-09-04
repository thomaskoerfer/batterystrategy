# Battery Strategy Architecture

This document defines the target architecture and the migration rules for
Battery Strategy. Contributors should read it before changing forecasting,
optimization, history access, plan directives, or live battery control.

The normative units, data semantics and executable layer boundaries are defined
in [INTERFACE_CONTRACTS.md](INTERFACE_CONTRACTS.md) and the `contracts` package.
They are binding migration guidelines, but deliberately evolvable. Any contract
change requires the impact analysis and explicit owner approval defined there
before implementations are adapted. Local workarounds that bypass a boundary
are not an acceptable form of contract evolution.

## Safety invariant

There is exactly one battery actuation path. Evaluation and diagnostics never
receive an actuator reference. Only the live controller may call the actuator.

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

## Documentation and agent-guidance gate

Every architecture layer has two maintained artifacts:

- a public `README.md` that explains purpose, contracts, inputs, outputs,
  non-responsibilities, supported capability classes, verification and current
  migration debt;
- an `AGENTS.md` that tells coding agents which responsibilities, dependencies
  and checks are allowed inside that boundary.

The layer index is [docs/README.md](docs/README.md). A migration phase is not
complete until the guides and agent instructions for every affected layer match
the implementation. Interface-contract changes still require the impact
analysis and explicit owner approval defined in `INTERFACE_CONTRACTS.md`.

The maintained layer guides are:

- [data adapters and feature store](docs/data-feature-store/README.md);
- [forecasting](docs/forecasting/README.md);
- [market context](docs/market-context/README.md);
- [optimization](docs/optimization/README.md);
- [planning service](docs/planning-service/README.md);
- [planning runtime](docs/planning-runtime/README.md);
- [plan compiler](docs/plan-compiler/README.md);
- [live control](docs/live-control/README.md);
- [actuation](docs/actuation/README.md);
- [measured savings](docs/savings/README.md);
- [evaluation and diagnostics](docs/evaluation/README.md).

Public documentation and committed agent guidance must describe normalized
roles and capability classes. They must not contain installation-specific
entity IDs, names, addresses, hostnames, URLs, serial numbers, credentials or
local filesystem paths. A currently supported vendor or provider class may be
named when support is genuinely limited to it.

While implementation modules share one package directory, its local
`AGENTS.md` maps modules to their architecture owner. When a layer is extracted
into its own package, its agent file moves with it. CI verifies that every layer
keeps both artifacts and that common setup-specific identifiers do not leak
into them.

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
generation and owns current plant limits, weather adjustment and learned slot
bias. Historical plant changes belong to explicit backtest preparation, not the
operational forecast contract. Both forecasters return point estimates,
uncertainty and data-quality metadata in a shared `ForecastBundle`.

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

The planning application publishes that same canonical plan in an immutable
`PlanningResult` together with a separate operator projection. The compiler
consumes only the canonical plan. Dashboard profiles and diagnostics remain
non-authoritative and are never parsed back into executable permission.

### Plan compiler

The plan compiler converts the economic `BatteryPlan` into explicit slot-bound
`PlanLiveDirective` values: PV charge permission, required charge, grid-charge
permission, discharge budget, SoC bounds and validity timestamps. The live
controller must not infer commercial intent or charge sources from planned power,
forecast PV or prices. The optimizer publishes PV charge, grid charge and the
required total charge separately. The compiler accounts for measured progress
but does not defer an optimizer action to another slot; equal-value scheduling
and uncertainty-aware optionality belong to the optimizer so the published plan
remains executable.

The active-slot commitment, rolling-replan and progress-accounting semantics
are normative in [the plan compiler guide](docs/plan-compiler/README.md).

### Live controller

The live controller runs on the fast coordinator interval. It combines the
current directive with live grid, PV, EV, battery and SoC measurements. It owns
meter following, EV policy, manual controls, budget consumption, command
smoothing and fail-safe behavior. It does not optimize prices or retrain a
forecast.

The Home Assistant adapter creates one `LiveMeasurements` snapshot and one
`LivePolicy`. The pure controller returns `LiveControlResult`, keeping its
validated `BatteryCommand`, explicit `LiveControlState` and `LiveDiagnostics`
separate. The actuator receives that command unchanged. No second live input,
directive or command model exists in production.

Dashboard future profiles remain canonical optimizer output. They may be joined
to measured history at the current timestamp, but the current live command does
not mutate future plan slots. This keeps plan diagnostics reproducible and
prevents display-only SoC shifts from becoming inconsistent with planned power.

Operator-mode precedence, PV-follow, EV treatment, manual override and disabled
control are normative in [the live control guide](docs/live-control/README.md).

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
choice must not affect forecasting or optimization semantics. Bounded history
is read through Home Assistant's public history API in the data adapter; no
downstream layer receives a recorder engine or depends on recorder tables.

## Current implementation

Forecast composition, market enrichment, optimization, planning orchestration,
plan compilation, live policy, actuation and measured savings have explicit
implementation owners. `planning_adapter.py` captures Home Assistant data,
`planning_runtime.py` freezes one refresh, the `runtime_*` adapters normalize
measurements and tariffs, and `planning_pipeline.py` coordinates the remaining
owners without reimplementing their rules. State, forecast invocation and
evaluation, and Home Assistant presentation have separate application modules.
The coordinator never writes hardware directly; `actuator.py` is the sole Home
Assistant battery-service writer. There are no compatibility facades,
alternative optimizers, compilers, forecast runners or hardware writers.

Home Assistant owns each loaded runtime through typed
`ConfigEntry.runtime_data`; integration services are registered once at domain
setup. The coordinator builds one immutable operator projection per refresh so
entity property reads perform no planning, conversion or wall-clock work.
Changing profile attributes are exposed to dashboards but excluded from
Recorder storage. `compiler_runtime.py` owns active-slot commitment, measured
progress and restart continuity behind one internal
interface. Its state is stored through the small versioned HA adapter in
`compiler_runtime_store.py` so a reload cannot reopen already consumed economic
permission. If an unclean restart cannot reconstruct progress from monotonic
battery counters, paid charge and discharge fail closed for the rest of that
slot while live PV-follow remains available. The coordinator retains HA
lifecycle, scheduling and the single actuator call; this extraction does not
change the compiler or live-control contracts.

Planning state schema 10 stores the canonical `BatteryPlan` beside operator
data. Older or malformed display snapshots remain readable but fail closed for
control until a fresh optimizer result exists. `planning_result.py` owns this
single persistence codec and the separation between canonical intent and
operator projection.

Configuration defaults and numeric constraints are owned once in
`config_definitions.py`; profile-aware entry validation is owned by
`config_validation.py`. Config flows and control entities present those rules
without duplicating them. Stored option keys, config-entry versions and entity
identities remain stable.

## Migration plan and gates

Current status: `0.2.0-rc.6` is deployed. The finalized feature store, pure
optimizer and deterministic plan compiler are authoritative, and all completed
shadow and compatibility paths have been removed. The `0.2.0-rc.7` candidate
hardens the Home Assistant boundary, Recorder hygiene, active-slot restart
continuity and concrete actuator conformance without changing economic,
forecast, compiler or live-control contract semantics. Deployment remains
subject to candidate validation and explicit owner approval.

### Phase 0: Baseline and contracts

- Freeze representative historical scenarios and current regression output.
- Introduce typed contracts for historical features, forecasts, optimization
  inputs, battery plans and evaluation results.
- Treat these contracts as the reviewed starting baseline; revise them through
  impact analysis when migration evidence exposes an incorrect boundary.
- Add forecast MAE/bias, plan parity and data-quality diagnostics.
- Change no production decisions.

Gate: all existing tests pass and serialized plan/live outputs are unchanged.

### Phase 0.5: Forecast seam and shadow parity

- Keep the existing forecast authoritative and duplicate only its pure
  mathematics in isolated load/PV shadow modules.
- Capture one immutable input set per optimizer run so production and shadow do
  not perform separate recorder, weather or state reads.
- Emit target-contract P50 forecasts; leave P10/P90 uncalibrated until matured
  residuals exist.
- Compare slot grids and P50 values without feeding shadow output into the
  optimizer, plan compiler, live controller or actuator.
- Retain one compact comparison per quarter-hour for 14 days outside the Home
  Assistant recorder.

Gate: at least 72 hours with identical slot grids, no unexplained shadow error,
and at most 1 W load/PV difference per slot. Production plans, commands and
actuation must remain unchanged. The transitional legacy sample input is not a
replacement for the finalized feature-store contract.

### Phase 1: Cut over extracted forecasting without changing mathematics

- Make the parity-proven load and PV modules authoritative for optimizer input.
- Keep coefficients, weather inputs and bias updates identical.
- Retain the previous path only for a short rollback period, then remove the
  duplicate forecast mathematics.

Gate: slot outputs remain numerically identical apart from explicit rounding,
live plans remain stable, and rollback to the old forecast path has been tested.
After this gate, remove the inline forecast calculation, parity gating and
legacy trace migration in one cleanup change; do not let the rollback path
become a second permanent production implementation.

### Phase 2: Add the feature store in parallel

- Aggregate live measurements into finalized 15-minute records.
- Persist versioned, compressed records with missing-data and coverage flags.
- Migrate existing learned samples without resetting bias state.
- Continue using recorder history as the production source.

Gate: at least seven days with complete slot coverage, bounded disk growth and
no effect on commands, forecasts or savings.

### Phase 3: Shadow recorder-independent forecasting

- Feed the extracted forecasters from the feature store in shadow mode.
- Invoke the shadow forecaster directly from a dedicated runner; feature history
  and shadow results never pass through optimization.
- Compare history-derived and feature-store-derived forecasts and backtests.
- Repair discrepancies in aggregation, restart handling and unit conversion.
- Keep load and PV implementations, configuration and error diagnostics
  independent. Compose total EV-free load from explicit named components so
  separately metered devices can later evolve without changing PV or unrelated
  base-load logic.
- Fetch normalized weather once through the weather adapter and pass one
  immutable slot snapshot to forecasters. No component performs network I/O.
- Configure independently metered loads as config subentries. Initial profiles
  split heat-pump DHW and space heating, model one shared AC outdoor-unit meter
  with multiple indoor contexts, and support a generic metered consumer.

Gate: at least seven complete days including weekdays and a weekend. Load and PV
are assessed independently against identical actual slots by lead time, time of
day, MAE, bias and daily energy. Missing data is excluded rather than treated as
zero. Phase 4 requires a separate owner approval after joint review; it never
starts automatically.

### Phase 4: Cut forecasting over to the feature store

- Make the feature store the production forecast source.
- Compose one immutable `ForecastBundle` before optimization; the optimizer may
  not read feature history or select a forecast implementation.
- Fail closed when the production history gate is not met. Do not add a hidden
  Recorder fallback or a permanent old/new runtime selector.
- Retain recorder access only for bootstrap/backfill through one adapter.
- Keep the existing optimizer and live controller unchanged.

Gate: local contract/regression tests and retained-history replay pass; at least
672 valid load slots, 672 valid PV slots, seven days of span and, when configured,
672 complete component slots exist. Deployment then requires explicit owner
confirmation. Several days of stable live operation and forecast metrics are
required before old recorder-query bootstrap code is removed.

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

Phase 7 is the final transformation phase, but not the end of normal product
development. Completion means every target boundary is authoritative and the
superseded path is gone; a local cleanup branch alone does not satisfy the gate.

## Refactoring rule

Each phase is released and observed before the next cutover. Refactoring must
preserve behavior first; forecast or optimization improvements are separate,
measurable changes after the corresponding boundary is stable.

Documentation is part of the refactoring gate, not follow-up work. An affected
layer README, its agent guidance and the architecture documentation must be
updated in the same change as the implementation.

Contract conformance is enforced at the boundary currently being migrated, not
retroactively across the entire legacy runtime. Discovering a deficient
contract pauses that boundary's cutover until its impact analysis, contract
tests and migration approach have been reviewed.

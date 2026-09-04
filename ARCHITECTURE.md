# Battery Strategy Architecture

This document defines the production architecture and evolution rules for
Battery Strategy. Contributors should read it before changing forecasting,
optimization, history access, plan directives, or live battery control.

The normative units, data semantics and executable layer boundaries are defined
in [INTERFACE_CONTRACTS.md](INTERFACE_CONTRACTS.md) and the `contracts` package.
They are binding guidelines, but deliberately evolvable. Any contract
change requires the impact analysis and explicit owner approval defined there
before implementations are adapted. Local workarounds that bypass a boundary
are not an acceptable form of contract evolution.

## Safety invariant

There is exactly one battery actuation path. Evaluation and diagnostics never
receive an actuator reference. The pure live controller returns a validated
command; only the Home Assistant coordinator calls the actuator.

## Production data flow

```text
HA measurements, recorder, weather ---> Data and feature store ---> Forecasting
                                                                          |
Market providers --------------------> Market context ----------------------+
                                                                          v
Battery state and configuration ------------------------------------> Optimization
                                                                          |
                                                                          v
                                                  Execution control (compiler + live)
                                                                          |
                                                                          v
                                                                       Actuator
```

Evaluation, diagnostics and backtesting observe the typed outputs of each layer.
They do not participate in live actuation.

## Documentation and agent-guidance gate

Every architecture component has two maintained artifacts:

- a public `README.md` that explains purpose, contracts, inputs, outputs,
  non-responsibilities, supported capability classes and verification;
- an `AGENTS.md` that tells coding agents which responsibilities, dependencies
  and checks are allowed inside that boundary.

The component index is [docs/README.md](docs/README.md). A change is not complete
until the guides and agent instructions for every affected component match the
implementation. Interface-contract changes still require the impact analysis
and explicit owner approval defined in `INTERFACE_CONTRACTS.md`.

The five production layers are:

1. data and feature store;
2. forecasting;
3. optimization;
4. execution control, comprising plan compiler and live control;
5. actuation.

Maintained component guides also cover market input, application orchestration,
measured savings, evaluation and diagnostics:

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
`AGENTS.md` maps modules to their architecture owner. CI verifies that every
documented component keeps both artifacts and that common setup-specific
identifiers do not leak into them.

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

### Execution control

Execution control contains two deliberately separate responsibilities. The plan
compiler preserves the optimizer's slot-level commercial commitment; live
control converts that commitment and current measurements into a safe command.
Neither responsibility may duplicate optimization or hardware translation.

#### Plan compiler

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

#### Live controller

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

The adapter evaluates freshness according to each source contract. Continuous
grid feedback has a strict report-age limit. Change-driven SoC, EV and battery
states remain valid while their entities are available and numerically valid;
an unchanged value is not evidence of stale data. Availability bridges begin
only after an entity becomes unavailable or invalid. Adding a time-based expiry
to a change-driven source requires an independent heartbeat or observation
timestamp plus the normal contract-change approval process.

Dashboard future profiles remain canonical optimizer output. They may be joined
to measured history at the current timestamp, but the current live command does
not mutate future plan slots. This keeps plan diagnostics reproducible and
prevents display-only SoC shifts from becoming inconsistent with planned power.

Operator-mode precedence, PV-follow, EV treatment, manual override and disabled
control are normative in [the live control guide](docs/live-control/README.md).

### Actuator

The actuator is the only hardware-writing boundary. It translates a validated
live command into vendor controls and enforces write throttling and safe zeros.

## Supporting components

- Market context normalizes provider data and enriches commercial policy before
  optimization. It is an input adapter, not a sixth decision layer.
- Planning service and planning runtime capture one immutable run and orchestrate
  calls across the five layers. They own no forecasting, economic or live rule.
- Measured savings, evaluation, diagnostics and backtesting observe published
  inputs and outputs. They cannot authorize a plan, command or hardware write.

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
resolves provider aliases and units, and privately retains Recorder entity IDs.
`planning_runtime.py` freezes only typed domain observations, role-keyed history,
normalized tariffs and forecast inputs for one refresh. The snapshot contains no
Home Assistant state mapping, entity ID, storage path or persistence service.
`planning_pipeline.py` coordinates the remaining owners without reimplementing
their rules. State, forecast invocation and
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

Planning state schema 11 stores the canonical `BatteryPlan` beside operator
data. `PlanningStateStore` owns one atomic document while exposing typed,
domain-owned forecast, simulation, market, savings and publication state to the
planning application. Older or malformed display snapshots remain readable but
fail closed for control until a fresh optimizer result exists.
`planning_result.py` owns the canonical-plan codec and keeps fresh typed
projection separate from the display-only restore parser.

Every planning refresh has exactly one adapter-captured `captured_at_ms`.
Planning, history cutoffs, price selection, forecast generation and persistence
ordering derive time from that snapshot; downstream planning modules do not
read the wall clock. Recorder history is completed in the executor. The adapter
then loads typed owner state, runs planning, persists the returned state and
publishes the result; persistence ownership is not part of the planning snapshot.

Configuration defaults and numeric constraints are owned once in
`config_definitions.py`; profile-aware entry validation is owned by
`config_validation.py`. Config flows and control entities present those rules
without duplicating them. Stored option keys, config-entry versions and entity
identities remain stable.

## Evolution and verification

The architecture above is the production design. There is one authoritative
implementation at every decision boundary; experiments and evaluations are
non-authoritative and cannot reach actuation.

Refactoring must preserve behavior first. Forecast or optimization improvements
are separate, measurable changes with explicit regression and live-observation
criteria.

Documentation is part of the refactoring gate, not follow-up work. An affected
layer README, its agent guidance and the architecture documentation must be
updated in the same change as the implementation.

Discovering a deficient contract pauses implementation until its impact
analysis, contract tests and compatibility approach have been reviewed and the
owner has explicitly approved the change.

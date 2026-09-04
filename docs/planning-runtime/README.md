# Planning runtime

## Purpose

The planning runtime is the Home Assistant application boundary around one
planning refresh. `planning_adapter.py` captures normalized Home Assistant
state and bounded history. `planning_pipeline.py` coordinates forecasting,
market context, optimization, measured savings and profile publication through
their owning components and returns the established integration payload.

It is orchestration, not a domain layer. Forecast equations, market policy,
optimization decisions, compiler semantics and live commands belong to their
respective components.

## Inputs and outputs

The adapter captures live inputs, configured strategy options, finalized
feature history, weather and device-component context. `PlanningRuntime`
validates and freezes that complete snapshot for exactly one refresh. No
runtime input or configuration is written to module globals.

The runtime is split by application responsibility:

- `runtime_measurements.py` provides normalized state and bounded history
  views;
- `runtime_market_data.py` normalizes the captured tariff intervals;
- `planning_state.py` owns versioned state loading, saving and virtual state;
- `forecast_application.py` invokes the forecast contract and
  `forecast_evaluation.py` matures prior forecast observations;
- `planning_pipeline.py` sequences the component calls;
- `plan_presentation.py` creates the stable Home Assistant profile and dispatch
  representation.

The Home Assistant integration owns each coordinator through typed config-entry
runtime data and registers domain services once, independently of entry reloads.
`operator_projection.py` converts the completed coordinator result into one
immutable set of entity values and dashboard attributes per refresh. Entity
properties only read that projection; they never repeat profile construction,
time conversion or planning fallbacks. Large changing profile attributes are
excluded from Recorder while remaining available to the current dashboard.

The pipeline returns one immutable `PlanningResult`. It carries the optimizer's
canonical `BatteryPlan` separately from the established `StrategyPlan`,
diagnostics, profiles and measured-savings projection. Only the canonical plan
may authorize compilation; presentation mappings are never converted back into
an executable plan. It does not expose a command-line or stdout JSON protocol.

The serialized planning state uses schema 10 and stores the canonical plan next
to operator data. Schema-9 display data remains readable after upgrade, but it
cannot authorize control and fails closed until the optimizer publishes a new
canonical plan. Invalid plans and plans built with different physical battery
constraints follow the same rule. This is a one-time data migration, not an
alternative runtime implementation or compatibility planning path.

## Setup independence

Runtime entity IDs and installation coordinates are supplied only by the Home
Assistant config entry. Source, examples, tests and diagnostics must not embed
private installation identifiers or endpoints.

## Verification

Run the complete test suite, retained-history optimizer replay, current-horizon
parity replay, architecture boundary tests and Home Assistant integration tests
after changing this boundary. Tests must also prove that two independently
constructed runtime snapshots cannot modify each other's settings or inputs.
Config-entry lifecycle tests must prove runtime-data ownership, one-time service
registration and planner shutdown ordering.

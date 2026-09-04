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

The adapter captures live inputs, configured strategy options, normalized
tariffs, finalized feature history, weather and device-component context.
`PlanningRuntime` contains only immutable domain values for exactly one refresh:
typed observations, role-keyed history, a tariff schedule and forecast inputs. It
never exposes Home Assistant states, provider payload aliases, entity IDs,
filesystem paths or a persistence object. No
runtime input or configuration is written to module globals. Its required
`captured_at_ms` is the sole observation time for all downstream cutoffs,
slot selection, forecast generation and persistence ordering.

Battery charge and discharge remain separate non-negative observation fields;
the orchestration derives a signed legacy calculation value only at its point of
use. Provider metadata cannot redefine the authority of normalized retail
tariffs.

The runtime is split by application responsibility:

- `runtime_measurements.py` provides role-keyed bounded history views and
  derived measurement profiles;
- `runtime_market_data.py` defines the normalized immutable tariff schedule;
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

Recorder entity resolution remains private to `PlanningCapture`. The adapter
completes the snapshot with Recorder history in the executor, then loads owner
state, invokes the planning orchestration, saves owner state and only then
publishes the result. This preserves Home Assistant's event-loop boundary and
keeps persistence lifecycle and leases out of planning inputs.

The serialized planning state uses schema 11 and stores the canonical plan next
to operator data. `PlanningStateStore` is the sole migration, validation and
atomic-write owner. It exposes separate typed owner state for forecast learning,
simulation, market enrichment, savings and publication without splitting the
on-disk document. A lifecycle lease and captured-time check prevent an obsolete
coordinator or older planning run from overwriting newer state. Schema-9/10
display data remains readable after upgrade, but it
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
constructed runtime snapshots cannot modify each other's settings or inputs,
and that raw Home Assistant/provider mappings cannot cross the adapter seam.
Config-entry lifecycle tests must prove runtime-data ownership, one-time service
registration and planner shutdown ordering.

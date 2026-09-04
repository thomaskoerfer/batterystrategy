# Planning runtime agent rules

## Allowed

- Capture Home Assistant inputs in `planning_adapter.py` and freeze each run in
  `planning_runtime.py`.
- Convert Home Assistant states, provider aliases and units into typed domain
  observations and tariffs at the adapter seam.
- Sequence existing component APIs in `planning_pipeline.py`.
- Keep state migration/persistence in `planning_state.py`, forecast invocation
  and evaluation in their application modules, and entity/profile projection in
  `plan_presentation.py`.
- Keep HA entity projection in `operator_projection.py`; sensor properties may
  only read the precomputed projection. Own coordinators through typed config
  entry runtime data and register integration services at domain setup.
- Return `PlanningResult` from the planning pipeline. Preserve the canonical
  `BatteryPlan` for the compiler and keep operator data non-authoritative.
- Require one adapter-captured `captured_at_ms`; downstream planning code must
  derive all time decisions from it.
- Keep one `PlanningStateStore` as the atomic persistence owner and mutate only
  the typed owner state belonging to the current component.
- Keep Recorder entity IDs in the private `PlanningCapture`; complete history
  in the executor before planning. Execute in this order: capture, read history,
  load owner state, plan, save owner state, publish.

## Forbidden

- Reimplement forecast, market, optimizer, compiler, live-control or actuator
  rules.
- Add compatibility wrappers, subprocess/CLI protocols, stdout transport or a
  second planning path.
- Access Recorder schemas, database engines or battery services directly.
- Store per-entry configuration, state, history, weather or prices in module
  globals.
- Put raw Home Assistant state mappings, provider payloads, entity IDs,
  configuration paths or `PlanningStateStore` inside `PlanningRuntime`.
- Recompute profiles, timestamps or planning fallbacks from entity properties,
  or expose large changing profile attributes to Recorder.
- Reconstruct a `BatteryPlan` or compiler permission from `StrategyPlan`,
  profiles, diagnostics or persisted display-only data.
- Read the wall clock after the adapter has captured the planning runtime.

## Required checks

Run all unit and contract tests, architecture tests, retained-history and
current-horizon parity replays, HACS validation and hassfest. Any contract
change additionally requires an impact analysis and explicit owner approval.

## Setup independence

Keep entity IDs, hostnames, coordinates, credentials and device identifiers in
runtime configuration only. Fixtures and documentation must remain portable.

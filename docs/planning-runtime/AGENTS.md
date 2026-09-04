# Planning runtime agent rules

## Allowed

- Capture Home Assistant inputs in `planning_adapter.py` and freeze each run in
  `planning_runtime.py`.
- Normalize only captured measurements and tariffs in the two `runtime_*`
  modules.
- Sequence existing component APIs in `planning_pipeline.py`.
- Keep state migration/persistence in `planning_state.py`, forecast invocation
  and evaluation in their application modules, and entity/profile projection in
  `plan_presentation.py`.
- Keep HA entity projection in `operator_projection.py`; sensor properties may
  only read the precomputed projection. Own coordinators through typed config
  entry runtime data and register integration services at domain setup.

## Forbidden

- Reimplement forecast, market, optimizer, compiler, live-control or actuator
  rules.
- Add compatibility wrappers, subprocess/CLI protocols, stdout transport or a
  second planning path.
- Access Recorder schemas, database engines or battery services directly.
- Store per-entry configuration, state, history, weather or prices in module
  globals.
- Recompute profiles, timestamps or planning fallbacks from entity properties,
  or expose large changing profile attributes to Recorder.

## Required checks

Run all unit and contract tests, architecture tests, retained-history and
current-horizon parity replays, HACS validation and hassfest. Any contract
change additionally requires an impact analysis and explicit owner approval.

## Setup independence

Keep entity IDs, hostnames, coordinates, credentials and device identifiers in
runtime configuration only. Fixtures and documentation must remain portable.

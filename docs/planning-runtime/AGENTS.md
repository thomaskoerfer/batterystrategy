# Planning runtime agent rules

## Allowed

- Capture normalized Home Assistant snapshots in `planning_adapter.py`.
- Sequence existing component APIs in `planning_pipeline.py`.
- Publish the stable integration payload and bounded diagnostics.

## Forbidden

- Reimplement forecast, market, optimizer, compiler, live-control or actuator
  rules.
- Add compatibility wrappers, subprocess/CLI protocols, stdout transport or a
  second planning path.
- Access Recorder schemas, database engines or battery services directly.

## Required checks

Run all unit and contract tests, architecture tests, retained-history and
current-horizon parity replays, HACS validation and hassfest. Any contract
change additionally requires an impact analysis and explicit owner approval.

## Setup independence

Keep entity IDs, hostnames, coordinates, credentials and device identifiers in
runtime configuration only. Fixtures and documentation must remain portable.

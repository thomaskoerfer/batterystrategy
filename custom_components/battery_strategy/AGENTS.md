# Battery Strategy source boundaries

Read the root `ARCHITECTURE.md`, `INTERFACE_CONTRACTS.md` and the relevant layer
guide under `docs/` before editing this package. Contract changes require a
documented impact analysis and explicit owner approval.

## Current module ownership

- Data and feature store: `feature_store.py`, `weather.py`,
  `load_components.py`, `component_config.py` and configuration adapters.
- Forecasting: the `forecasting` package and forecast composition runners.
- Optimization: `economic_optimizer.py` and `optimization_problem.py`;
  orchestration and publication code remaining in `optimizer_engine.py`,
  `optimizer_adapter.py` and `planner.py` is transitional.
- Plan compiler: `plan_compiler.py`; compiler orchestration still present in
  coordinator/strategy code is transitional.
- Live control: `live_control.py`, `strategy.py` and live orchestration in
  `coordinator.py`.
- Actuation: `actuator.py` and actuator wiring only.
- Evaluation: diagnostics, bounded command traces, backtests and
  measured-savings reporting.

## Boundary rules

- Keep one hardware writer. Shadow and evaluation code never receive an
  actuator reference.
- Keep forecasting free of prices and battery state; keep optimization free of
  Home Assistant and I/O; keep live control free of economic re-optimization.
- Use contract types at boundaries and explicit state instead of module globals.
- Do not spread transitional dependencies. Remove them after their documented
  observation and rollback gate.
- Update the affected public README and agent guidance with implementation.

## Required checks

Run formatting/static checks, all unit tests owned by changed layers, contract
tests, architecture-documentation tests and any retained-history parity suite
required by the migration phase.

## Setup independence

Committed source guidance, tests and public examples must not contain concrete
installation identifiers, credentials or private endpoints. Runtime config and
redacted diagnostics are the only places that map normalized roles to a user's
installation.

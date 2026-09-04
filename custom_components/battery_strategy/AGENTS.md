# Battery Strategy source boundaries

Read the root `ARCHITECTURE.md`, `INTERFACE_CONTRACTS.md` and the relevant layer
guide under `docs/` before editing this package. Contract changes require a
documented impact analysis and explicit owner approval.

## Current module ownership

- Data and feature store: `feature_store.py`, `weather.py`,
  `load_components.py`, `component_config.py` and configuration adapters.
- Forecasting: the `forecasting` package and forecast composition runners.
- Market context: `market_context.py`; provider enrichment and commercial
  price context must not enter forecasting or the pure optimizer.
- Optimization: `economic_optimizer.py` and `optimization_problem.py`.
- Planning application: `planning_service.py`; invoke the optimizer once and
  publish its plan without constructing forecasts.
- Measured savings: `savings.py`; actual accounting is observational and must
  not influence planning or live control.
- Home Assistant planning boundary: `planning_adapter.py` captures normalized
  runtime inputs; `planning_runtime.py` freezes one run snapshot;
  `runtime_measurements.py` and `runtime_market_data.py` normalize captured
  input; `planning_state.py` owns persisted application state;
  `forecast_application.py` and `forecast_evaluation.py` call forecast-owned
  APIs; `planning_pipeline.py` only coordinates; and `plan_presentation.py`
  publishes the stable entity payload. None may own another layer's rules.
- Plan compiler: `plan_compiler.py`; `compiler_runtime.py` owns active-slot
  progress, commitment and restart continuity around it. The coordinator
  supplies measurements but may not recreate these semantics.
- Planning result: `planning_result.py` preserves the canonical `BatteryPlan`
  separately from immutable operator projections and owns its versioned state
  codec. Presentation data must never be converted back into compiler intent.
- Plan-compiler persistence adapter: `compiler_runtime_store.py`; it may store
  explicit compiler state and measured progress but may not interpret prices or
  create permission.
- Live control: `live_control.py`, `strategy.py` and live orchestration in
  `coordinator.py`.
- Actuation: `actuator.py`; it is the only module allowed to call Home
  Assistant services for battery hardware.
- Evaluation: `forecast_evaluation.py`, `command_trace.py`, diagnostics,
  backtests and measured-savings reporting.
- Home Assistant operator projection: `operator_projection.py` precomputes
  entity values and non-recorded dashboard attributes once per refresh;
  `sensor.py` is a read-only entity adapter.
- Home Assistant configuration: `config_definitions.py` owns option defaults
  and numeric constraints, while `config_validation.py` owns profile-aware
  validation. Flow and control-entity modules may present but not duplicate
  those rules.

## Boundary rules

- Keep one hardware writer. Evaluation code never receives an
  actuator reference.
- Apply source-aware freshness: strict report age is for continuous grid
  feedback; available and valid change-driven SoC, EV and battery states do not
  expire merely because their value is unchanged. Changing this rule requires
  an independent heartbeat/timestamp contract, impact analysis and explicit
  owner approval.
- Keep forecasting free of prices and battery state; keep optimization free of
  Home Assistant and I/O; keep live control free of economic re-optimization.
- Use contract types at boundaries and explicit state instead of module globals.
- Do not introduce duplicate input, directive or command models at an existing
  contract seam. Projection models never authorize execution.
- Update the affected public README and agent guidance with implementation.

## Required checks

Run formatting/static checks, all unit tests owned by changed layers, contract
tests, architecture-documentation tests and relevant retained-history replays.

## Setup independence

Committed source guidance, tests and public examples must not contain concrete
installation identifiers, credentials or private endpoints. Runtime config and
redacted diagnostics are the only places that map normalized roles to a user's
installation.

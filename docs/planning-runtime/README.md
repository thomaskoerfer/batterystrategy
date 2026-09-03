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

The adapter receives immutable live inputs, configured strategy options,
finalized feature history, weather and device-component context. The pipeline
returns the current plan, diagnostics, profiles and measured-savings values as
one in-process Python mapping. It does not expose a command-line or stdout JSON
protocol.

## Setup independence

Runtime entity IDs and installation coordinates are supplied only by the Home
Assistant config entry. Source, examples, tests and diagnostics must not embed
private installation identifiers or endpoints.

## Verification

Run the complete test suite, retained-history optimizer replay, current-horizon
parity replay, architecture boundary tests and Home Assistant integration tests
after changing this boundary.

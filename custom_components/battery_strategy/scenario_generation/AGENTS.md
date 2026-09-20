# Scenario-generation source rules

Follow `docs/scenario-generation/README.md`, its adjacent `AGENTS.md`, the root
architecture and approved interface contracts.

This package owns only pure dependence-aware path generation. It may consume
immutable marginal forecasts and causal evidence, but never prices, battery
state, Home Assistant objects, persistence services, live state or actuators.

Any change to request/result semantics requires an impact analysis and explicit
owner approval. Preserve deterministic replay and structured diagnostics for
every repair, fallback and rejected path.

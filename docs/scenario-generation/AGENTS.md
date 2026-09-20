# Scenario-generation agent rules

Read this guide, the root architecture, interface contracts and the approved
scenario-builder impact analysis before changing this component.

## Allowed

Own causal evidence selection, temporal and cross-series dependence, bounded
path repair, marginal rank mapping, scenario weighting and diagnostics.

## Forbidden

Do not forecast marginal P50/P10/P90 values, read Home Assistant or persistence,
use prices or battery state, choose economic actions, compile a plan or call an
actuator. Do not hide missing evidence or interpolate EV sessions.

## Required checks

Run contract, scenario-builder, optimizer-shadow, architecture-boundary and
trace tests. Verify deterministic output for identical immutable requests and
report every repair or marginal fallback in diagnostics.

## Setup independence

Use normalized contract fields only. Never embed installation identifiers,
endpoints, credentials or local paths.

# Data and feature-store agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before working on this layer.

## Allowed

Own provider/entity adapters, units, signs, time alignment, quality flags,
historical aggregation, persistence, retention, schema migration and recorder
bootstrap. Emit normalized immutable facts.

## Forbidden

Do not forecast, value energy, allocate battery budgets or issue hardware
commands. Do not expose database engines, entity IDs or storage details through
downstream contracts. Do not treat missing data as measured zero.

## Required checks

Test unit and sign conversion, time weighting, gaps, counter resets, schema
migration, retention and recorder-backend independence. Demonstrate downstream
parity unless a separately approved contract change is intended.

## Setup independence

Use normalized source roles. Never add installation identifiers or private
connection details to public documentation, fixtures or committed guidance.

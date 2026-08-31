# Actuation agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before working on hardware writes.

## Allowed

Own generic-command translation, safe direction sequencing, write coalescing,
availability handling and observable actuation results. This is the sole
hardware-writing boundary.

## Forbidden

Do not change commercial intent, increase requested power, reverse direction or
let shadow/diagnostic code obtain an actuator reference. Disabled control writes
zero once and then stops writing.

## Required checks

Test direction changes, stale controls, duplicate suppression, fail-safe zero,
restart behavior, disabled no-write behavior and the single-writer invariant.

## Setup independence

Vendor adapters may name supported capability classes, never a concrete device,
entity mapping or private endpoint.

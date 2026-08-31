# Actuation

## Purpose

The actuator is the only boundary allowed to write battery hardware controls.
It translates a validated `BatteryCommand` into vendor-specific operations while
preserving direction, power and safety limits.

## Contract

`BatteryActuator.apply(command)` returns an observable `ActuationResult`. The
actuator may lower or reject a request for safety, availability or throttling,
but it may not increase power, reverse direction or invent commercial intent.

No optimizer, plan compiler, dashboard, diagnostic or shadow component may hold
an actuator reference.

## Write sequencing

Battery direction changes are not assumed atomic. A vendor adapter must safely
stop the opposite direction, change mode and then apply the new limit. Duplicate
writes are coalesced and small changes may be throttled, but a fail-safe zero or
direction stop must not be delayed by normal write optimization.

Control disable writes safe zero limits once and then stops writing. This allows
external manual operation until control is explicitly re-enabled.

## Current support

Active control currently supports Zendure-compatible Home Assistant entities
that expose AC input/output mode and input/output power limits. Generic battery
profiles may be monitored without actuation. Additional vendor adapters must
implement the same generic command contract and cannot leak vendor concepts into
optimization or live control.

## Setup independence

Vendor capability names may be documented, but public code guidance must not
contain a device serial number, entity ID, address, hostname, account URL or
installation-specific mode label. Runtime configuration maps generic actuator
roles to one installation's entities.

## Verification

Tests cover target translation, opposite-direction zeroing, stale/unavailable
controls, write coalescing, minimum deltas, restart behavior, disabled-control
no-write behavior and the single-writer invariant. Hardware cutovers require a
rollback release and the shortest practical Home Assistant interruption.

## Migration status

The vendor translation and write tracker are separated, while orchestration is
still partly coordinator-owned. Phase 6 preserves this proven path. Final
cleanup may move wiring but must not alter write order or safety semantics
without separate approval.

# Plan-compiler agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before changing compiler behavior. The documented slot-latching semantics
are owner-approved and normative.

## Allowed

Translate one economic plan plus measured progress and explicit prior state into
one slot-bound live directive and next compiler state.

## Forbidden

Do not read prices, forecasts, Home Assistant, persistence or the wall clock.
Do not create economic permission, move energy between slots or raise an active
slot commitment after it has been latched.

Home Assistant persistence belongs to the adjacent runtime adapter. It may
serialize the explicit state and measured progress but must not add compiler
rules or silently reopen permission.

## Required checks

Test progress accounting, within-slot lower-only replans, next-slot refresh,
required-charge source rules, mode switches, clean reload continuation,
counter-based crash recovery and restart fail-closed behavior.

## Setup independence

Consume contracts only; never add installation or vendor identifiers.

# Plan-compiler agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before changing compiler behavior. The documented slot-latching semantics
are owner-approved and normative.

## Allowed

Translate one economic plan plus measured progress and explicit prior state into
one slot-bound live directive and next compiler state.

## Forbidden

Do not read prices, forecasts, Home Assistant, persistence or the wall clock.
Do not create economic permission or move energy between slots. A discharge
commitment originating before the slot boundary is provisional and may be
replaced only by the first eligible post-boundary plan. Never raise it after
that explicit reconciliation has made it final. Required grid charge remains
lower-only throughout the active slot.

Home Assistant persistence belongs to the adjacent runtime adapter. It may
serialize the explicit state and measured progress but must not add compiler
rules or silently reopen permission.

## Required checks

Test progress accounting, one-time boundary reconciliation, subsequent
within-slot lower-only replans, next-slot refresh, required-charge source rules,
mode switches, clean reload continuation, counter-based crash recovery and
restart fail-closed behavior.

## Setup independence

Consume contracts only; never add installation or vendor identifiers.

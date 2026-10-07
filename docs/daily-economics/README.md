# Daily economics presentation

## Purpose

Daily economics presents comparable full-calendar-day cost projections. For
the current day it combines finalized EV-free actual grid cost and the
authoritative direct-counter savings ledger through one explicit cutoff with
future plan slots after that cutoff. The next day is plan-only.

## Boundaries

The component consumes normalized finalized features, the measured-savings
ledger result and an immutable operator plan. It performs no I/O, forecasting,
optimization, compilation or measured-savings accounting. Its output is
presentation-only and cannot authorize a battery command. Actual-only savings
and battery energy remain owned by the measured-savings ledger.

## Semantics

Optimized actual cost values the measured grid position after removing EV. The
actual no-battery baseline is that observed cost plus the authoritative savings
from direct battery energy counters; feature-store power integration must not
become a competing savings ledger. Planned cost uses the published load, PV and
grid flows. Import and export use the configured commercial values, and actual
and planned slots never overlap.
If any physical local-time slot is missing, including a missing actual price or
the extra hour of an incompletely covered DST day, that day's projection is
unavailable rather than a misleading partial sum.

## Verification

Tests cover the cutoff, EV exclusion, actual/plan composition, tomorrow's
plan-only projection and separation from measured-savings entities.

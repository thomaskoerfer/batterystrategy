# Daily economics presentation

## Purpose

Daily economics presents comparable full-calendar-day cost projections. For
the current day it combines finalized EV-free actual slots through one explicit
cutoff with future plan slots after that cutoff. The next day is plan-only.

## Boundaries

The component consumes normalized finalized features and an immutable operator
plan. It performs no I/O, forecasting, optimization, compilation or measured
savings accounting. Its output is presentation-only and cannot authorize a
battery command. Actual-only savings and battery energy remain owned by the
measured-savings ledger.

## Semantics

Baseline cost values the EV-free house load minus PV without battery behavior.
Optimized actual cost values the measured grid position after removing EV.
Planned cost uses the published load, PV and grid flows. Import and export use
the configured commercial values, and actual and planned slots never overlap.
If any physical local-time slot is missing, including a missing actual price or
the extra hour of an incompletely covered DST day, that day's projection is
unavailable rather than a misleading partial sum.

## Verification

Tests cover the cutoff, EV exclusion, actual/plan composition, tomorrow's
plan-only projection and separation from measured-savings entities.

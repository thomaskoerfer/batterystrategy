# Quantitative recharge reserve

Date: 2026-08-18

## Problem

The optimizer stopped reserving current battery inventory for later
higher-value household load at the first planned charge slot. A very small PV
charge therefore had the same effect as a complete recharge and could expose
the whole remaining battery inventory to earlier, cheaper live load.

## Change

Future planned charge is converted to expected deliverable AC energy and
consumed chronologically against later higher-value forecast load. Grid charge
is firm. PV charge uses the existing PV recovery confidence. Only uncovered
higher-value load remains reserved.

## Contract impact

The `BatteryPlan` schema, units, signs and invariants are unchanged. The
semantics of `discharge_budget_kwh` are narrowed to enforce its existing
commercial-permission contract: a future charge reduces scarcity only by the
energy it replaces. No producer or consumer migration is required.

## Layer impact

- Data and forecasting: unchanged.
- Optimization: corrected reserve calculation only.
- Plan compilation: unchanged.
- Live control, EV/PV policy and slot progress: unchanged.
- Actuation: unchanged.

## Tests and rollback

Regression coverage includes incomplete future charge, small PV charge with a
confidence discount and sufficient firm grid recharge. Rollback is the
`0.2.0-beta.10` tag.

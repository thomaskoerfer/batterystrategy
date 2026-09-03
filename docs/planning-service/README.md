# Planning service

## Purpose

The planning service is the thin application orchestrator for one immutable
planning snapshot. It combines an existing forecast and market context with
battery constraints, invokes the single pure optimizer once and adapts the
typed `BatteryPlan` into the stable published plan representation.

```text
ForecastBundle + price horizon + battery snapshot + configuration
    -> commercial policy -> OptimizationProblem -> optimizer -> published plan
```

It contains no forecast model, market network access, measured-savings
accounting, live meter following or hardware writes. Existing forecast,
optimization and plan contracts remain authoritative; `PlanningService` is an
internal application boundary, not a new public schema.

## Setup independence

The service accepts only normalized forecasts, market slots, battery state and
configuration. It cannot know entity identifiers, recorder backends, hardware
vendors or provider payloads. New installations are supported by adapters, not
branches in planning orchestration.

## Verification

Tests verify aligned grids, unchanged units and signs, daily-cost parity and
the absence of Home Assistant, network and forecast-construction dependencies.

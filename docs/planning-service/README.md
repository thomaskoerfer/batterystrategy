# Planning service

## Purpose

The planning service is the thin application orchestrator for one immutable
planning snapshot. It combines an existing forecast and market context with
battery constraints, invokes the single pure optimizer once and adapts the
typed `BatteryPlan` into stable operator metadata.

```text
ForecastBundle + price horizon + battery snapshot + configuration
    -> commercial policy -> OptimizationProblem -> optimizer -> published plan
```

It returns `PlanningPublication`: the exact canonical `BatteryPlan` plus
typed operator points, typed daily costs and non-authoritative presentation
metadata. Fresh publication constructs the typed projection directly from the
canonical plan and aligned forecast/market values. Dictionary parsing is
reserved for display-only startup restoration and can never authorize control.
The service contains no forecast model, market
network access, measured-savings accounting, live meter following or hardware
writes. It never reconstructs executable intent from presentation data.

The surrounding planning runtime combines the publication with diagnostics as
an immutable `PlanningResult`. Only its `battery_plan` member may enter the plan
compiler; the `StrategyPlan` and mapping projections exist for Home Assistant
entities and dashboards.

## Setup independence

The service accepts only normalized forecasts, market slots, battery state and
configuration. It cannot know entity identifiers, recorder backends, hardware
vendors or provider payloads. New installations are supported by adapters, not
branches in planning orchestration.

## Verification

Tests verify aligned grids, unchanged units and signs, daily-cost parity and
the absence of Home Assistant, network and forecast-construction dependencies.

# Optimization

## Purpose

Optimization converts prices, a `ForecastBundle`, current battery state,
physical constraints and commercial policy into an economic `BatteryPlan`.
It decides future energy allocation; it does not control instantaneous power.

## Pure interface

The target interface is:

```text
optimize(OptimizationProblem) -> BatteryPlan
```

The function is deterministic and side-effect free. It must not access Home
Assistant, entities, recorder history, files, network resources or the wall
clock. Every decision input is explicit in `OptimizationProblem`.

## Economic model

The optimizer considers:

- quarter-hour import and export valuation;
- forecast EV-free load and PV generation;
- current SoC, usable capacity and charge/discharge limits;
- round-trip efficiency and minimum commercial margin;
- terminal value and forecast horizon boundaries;
- PV headroom and expected spill;
- future higher-value household demand.

The objective uses real import cost, export opportunity cost and explicit policy
only. Ranks and heuristics must not appear as fictional currency credits.

## Plan semantics

Each `BatteryPlanSlot` contains planned charge and discharge energy, separate PV
and grid charge sources, required charge, commercial discharge budget and the
expected SoC trajectory. Charge and discharge cannot coexist in one slot.

Discharge budget is permission for the plan compiler and live controller, not a
power target. Planned discharge must fit within it. The optimizer never plans
battery export when export has no compensating value.

## Current implementation

The extracted implementation uses deterministic dynamic programming. The Home
Assistant adapter currently supplies quarter-hour market data and optional
longer-horizon market context. The pure optimizer itself is provider-neutral and
battery-vendor-neutral.

## Setup independence

No optimization rule may refer to a household, entity ID, tariff account,
address, serial number, hostname or local URL. Battery models and market
providers are represented only by normalized constraints, prices and policy.

## Verification

Golden-master and scenario tests cover RTE, uneconomic cycles, negative prices,
terminal value, horizon boundaries, PV headroom, source permissions, scarce
future energy, EV exclusion and deterministic tie-breaking. Perfect-foresight
replays assess economic quality but never participate in live actuation.

## Migration status

The pure optimizer currently runs in isolated shadow mode while the previous
economic kernel remains authoritative. Cutover requires retained-history parity,
live observation and explicit approval. The old kernel is removed after a short
verified rollback window rather than retained as a permanent selector.

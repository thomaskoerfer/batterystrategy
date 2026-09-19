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

With coherent scenarios, `StochasticDynamicProgrammingOptimizer` uses a
probability-weighted two-stage receding horizon: all paths share the first
battery transition and have independent optimal recourse afterwards. EV demand
remains separate and can affect PV allocation or discharge only through the
explicit EV interaction policy. Without scenarios it returns the deterministic
P50 plan exactly.

Only the shared first transition is executable scenario output. Remaining plan
slots are deterministic P50 recourse for safe restart continuity and are
replaced by rolling optimization before execution. `optimized_cost_eur` remains
the presentation plan's P50 energy bill; stochastic objective values are not
reported as accounting costs.

The preceding replacement shadow kept `DynamicProgrammingOptimizer`
authoritative. This prepared cutover generates scenarios in forecasting and
selects the stochastic optimizer at the optimization boundary. It changes
optimizer selection, not the `BatteryPlan`, compiler, live-control or actuation
contracts.

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

Energy substitution is chronological. A later charging opportunity can replace
an earlier one only for demand that occurs after that later charge. The dynamic
programming state already enforces this causality through the battery trajectory;
global future-capacity shortcuts must not reject otherwise feasible actions.
Equal-cost plans may still defer grid charging through deterministic tie-breaking,
but never beyond the demand that requires the stored energy.

Economic charge/discharge eligibility is resolved inside the optimization
objective. Publication may normalize lattice-sized source allocation, but it
must not delete an optimized discharge based on a guessed origin or age of the
stored energy; doing so can strand paid inventory and invalidate plan cost.

## Plan semantics

Each `BatteryPlanSlot` contains planned charge and discharge energy, separate PV
and grid charge sources, required charge, commercial discharge budget and the
expected SoC trajectory. Charge and discharge cannot coexist in one slot.

Discharge budget is permission for the plan compiler and live controller, not a
power target. Planned discharge must fit within it. The optimizer never plans
battery export when export has no compensating value.

## Current implementation

The extracted implementation uses two-stage stochastic dynamic programming
when coherent scenarios are available and deterministic P50 dynamic programming
as an explicit fallback. The Home Assistant adapter supplies quarter-hour
market data and optional longer-horizon market context. Both pure optimizers are
provider-neutral and battery-vendor-neutral.

## Setup independence

No optimization rule may refer to a household, entity ID, tariff account,
address, serial number, hostname or local URL. Battery models and market
providers are represented only by normalized constraints, prices and policy.

## Verification

Golden-master and scenario tests cover RTE, uneconomic cycles, negative prices,
terminal value, horizon boundaries, PV headroom, source permissions, scarce
future energy, EV exclusion and deterministic tie-breaking. Perfect-foresight
replays assess economic quality but never participate in live actuation.

## Prepared cutover status

The stochastic optimizer is authoritative when a valid scenario set is
available and the complexity guard permits it. Otherwise the same immutable
`OptimizationProblem` is solved by deterministic P50. There is one executable
plan and no runtime shadow plan in this branch.

The prepared cutover uses an explicit complexity guard. If the usable-capacity
range would require more than 1,200 first-action states at 0.025 kWh resolution,
the deterministic P50 optimizer remains authoritative and diagnostics report
`stochastic_complexity_guard`.

The stochastic first-stage action is authoritative only for a planning vintage
captured within 60 seconds of the current slot boundary. A later mid-slot
replan uses the deterministic P50 optimizer and reports
`stochastic_mid_slot_guard`; already-used energy and slot commitment remain the
compiler's responsibility.

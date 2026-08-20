# Battery Strategy Interface Contracts

The executable contracts live in
`custom_components/battery_strategy/contracts/`. This document defines their
semantics and evolution rules. The contracts describe the target boundaries;
the legacy runtime models remain transitional until each migration phase in
`ARCHITECTURE.md` is completed.

## Contract status

These contracts are binding design constraints for new code and for code moved
across a layer boundary during the architecture migration. They are the initial
baseline for the target architecture, not an assertion that every decision in
the baseline is permanently correct. Existing production code is only required
to conform after the corresponding migration gate has been completed.

Contracts may change when implementation experience, operational evidence or
new requirements show that a boundary is incomplete or wrong. A contract must
not be bypassed locally to accommodate such a finding. Change the contract
deliberately, document the impact, update affected producers and consumers, and
then migrate them through the normal release gates.

Documentation and the executable types form one contract. If they disagree,
the change is incomplete; neither representation silently overrides the other.

## Why Python contracts

Battery Strategy is an internal Python pipeline, not a network API. Frozen
dataclasses provide explicit data ownership and runtime validation, while
`typing.Protocol` keeps implementations replaceable without inheritance. JSON
Schema or a service API would add serialization and deployment complexity
without improving the current boundary.

Persisted feature-store records are the exception: their storage envelope must
carry `CONTRACT_SCHEMA_VERSION` and be migrated explicitly.

## Universal rules

1. Timestamps are UTC epoch milliseconds. Timezone names are passed separately
   for calendar features.
2. Every planning slot is the half-open interval `[start_ms, end_ms)` and exactly
   15 minutes. Slot starts align to UTC quarter-hours, including DST days.
3. Forecast and optimization use slot energy in `kWh`. Live control and
   actuation use instantaneous power in `W`.
4. Named flows are non-negative: import/export, charge/discharge and
   generation/consumption are separate fields. Signed power must be normalized
   inside a data adapter.
5. Market prices, resulting costs and savings are finite but may be negative.
6. House-load forecasts exclude EV charging. PV forecasts represent generation
   before battery and grid decisions.
7. Missing values use `None`; `NaN`, infinity and magic numeric sentinels are
   invalid. Missing or estimated observations carry `DataQuality` metadata,
   including an explicit missing-price flag when slot valuation is unavailable.
8. Producers return immutable, sorted slot tuples. Consumers must reject
   duplicate or misaligned grids rather than silently interpolate them.
9. All time-dependent functions receive `as_of_ms` explicitly. Pure layers do
   not read the wall clock.
10. No domain contract contains an HA object, entity ID, SQL engine, database
   URL, MQTT topic, vendor option or filesystem path.
11. Validation failures fail closed at the live boundary. They may produce
    diagnostics or an idle command, never an unconstrained hardware command.

## Boundary contracts

### Data adapters to forecasting

`HistoricalFeatureSlot` is one finalized actual slot. Its canonical quantities
are energy, including house load without EV, PV generation, EV charging, grid
flows and battery flows. `DataQuality.coverage` records observed time coverage;
flags explain counter resets, restart gaps, estimation or missing inputs.

The feature store may upsert a slot after late data repair, but consumers only
receive one version of each sorted slot key.

### Weather and market adapters

`WeatherDataProvider` returns normalized `WeatherSlot` values on the requested
grid. `MarketDataProvider` returns import and export valuation in `ct/kWh`.
Provider names and fallback provenance remain metadata; provider-specific data
formats never cross the boundary.

### Feature data to forecasting

`LoadForecaster` and `PvForecaster` are pure synchronous calculations. I/O is
completed before they are called. Both receive a `ForecastRequest` containing
the complete horizon and explicit generation time.

Each result identifies its model version and training cutoff. The cutoff may not
be later than generation time, preventing accidental future-data leakage in
backtests.

P50 is the required point forecast. P10 and P90 are added as a pair only after
they can be calibrated from matured forecast residuals; missing quantiles mean
"not calibrated", not zero uncertainty. A `ForecastBundle` is valid only when
load and PV use the identical slot grid.

Current device measurements that can explain near-term load are supplied as an
extensible `LoadForecastContext`. Adapters map entities to stable semantic
driver keys; the forecaster may ignore unknown drivers. The whole-house load
without EV remains the forecast target, and optional device measurements must
not be subtracted from it a second time.

### Forecasting and market data to optimization

`OptimizationProblem` is a complete deterministic optimizer input. Market and
forecast grids must match exactly. Battery state cannot be newer than the
problem's `as_of_ms`.

The optimizer returns a `BatteryPlan`. It may plan either charge or discharge in
one slot, never both. Every slot explicitly identifies whether PV and grid
charging are commercially allowed. `required_charge_kwh` is the non-deferrable
portion of planned charge and cannot exceed total planned charge.
`discharge_budget_kwh` is commercial permission, not a live power target. The
plan carries the battery constraints used during optimization so the compiler
does not query configuration behind the contract. Planned discharge is expected
execution and must be contained by that permission; a budget may be larger to
cover unexpected live household load. Future recharge reduces inventory
reserved for later higher-value load only by its expected deliverable energy;
the presence of a charge slot alone never resets scarcity.
PV-recovery discharge permission requires a physical storage-headroom shortage:
confidence-weighted future PV surplus must exceed remaining battery headroom and
the uncertainty reserve. Forecast grid export is diagnostic optimizer output;
by itself it must not authorize discharge because it can result from plan
discretization or an economic choice.

Published future profiles are canonical projections of `BatteryPlan`. Actual
history may be joined up to the observation time, but a live directive or live
command must never rewrite future plan SoC, power or budget values. Live
deviations become optimizer input at the next planning run instead.

### Optimization to plan compiler

The plan compiler combines `BatteryPlan` with measured `SlotProgress`. It may
reduce remaining required charge or discharge budget based on actual progress,
but it does not re-optimize prices.

`PlanLiveDirective` contains every permission the live controller needs:
allowed charge sources, source-specific power limits, remaining required charge,
remaining discharge budget, SoC bounds and slot validity.

### Plan compiler to live controller

`LiveMeasurements` contains one normalized fast snapshot. Battery charge and
discharge are separate positive fields so meter feedback cannot invert the
house-load reconstruction. EV power remains explicit and is interpreted only
through the supplied `LivePolicy`. Previous command state is supplied as
`LiveControlState`; the controller does not keep hidden mutable state.

The live controller computes a `BatteryCommand` without I/O. An idle command has
zero power; active commands have positive power and a short validity interval.
Every command references the directive that authorized it.

### Live controller to actuator

`BatteryActuator.apply()` is the only hardware-writing port. Implementations may
translate generic input/output modes to Zendure or another vendor and may
coalesce redundant writes. They must not change commercial intent or increase
the requested power.

## Ownership rules

| Concern | Owner |
| --- | --- |
| Entity mapping, signs and units | Data adapter |
| Recorder bootstrap and feature persistence | Feature store adapter |
| EV-free house-load reconstruction | Feature engineering |
| Load model and load bias | Load forecaster |
| PV capacity, weather model and PV bias | PV forecaster |
| Price spread, RTE, terminal value and PV headroom | Optimizer |
| Slot budgets and required-charge translation | Plan compiler |
| EV policy, meter following and stale-input safety | Live controller |
| Vendor modes, limits and write throttling | Actuator |
| Accuracy, savings and perfect-foresight comparison | Evaluation |

## Compatibility and evolution

- Every contract change requires an impact analysis in its pull request or
  commit description. Small additive changes may use a short analysis; breaking
  changes require an explicit migration and rollback plan.
- Additive optional fields with unchanged semantics are allowed within a
  contract schema version. Producers must not assume that all consumers use the
  field until their migration is complete.
- Changed units, signs, required fields, invariants or semantics are breaking
  changes. Persisted or externally serialized contracts require a new schema
  version and an explicit data migration. Internal in-memory contracts require
  coordinated producer/consumer migration even when no persisted schema version
  changes.
- Removing a field follows the expand-and-contract pattern: introduce the
  replacement, migrate and observe all consumers, then remove the old field in
  a later phase.
- Producers and consumers receive contract tests from both sides. A layer is not
  cut over until old and new boundary outputs pass historical golden-master and
  live shadow comparisons.
- Shadow components may emit contract values and diagnostics only. They cannot
  obtain a `BatteryActuator` reference.
- Contract types do not expose persistence serialization directly. Storage and
  diagnostics adapters own conversion to JSON or Home Assistant attributes.

### Required impact analysis

The analysis should be proportional to the change, but must answer each
applicable item:

1. **Reason and evidence:** What new observation or requirement makes the
   current contract insufficient?
2. **Semantic impact:** Do units, signs, time alignment, optionality, invariants
   or ownership change?
3. **Dependency impact:** Which producers, consumers, adapters, stored records,
   diagnostics, dashboards and tests are affected?
4. **Decision impact:** Can forecasts, plans, budgets, live commands, savings or
   safety behavior change? If not, how is parity demonstrated?
5. **Compatibility:** Is the change additive or breaking? Is a schema-version
   bump, dual-read/dual-write period or backfill required?
6. **Verification:** Which contract tests, historical scenarios, shadow metrics
   and live acceptance checks prove the change?
7. **Rollout and rollback:** In which migration phase is it introduced, and how
   can the previous contract and implementation be restored safely?

The impact analysis is a decision record, not ceremony: links to tests,
backtests or operational evidence are preferred over speculative prose. A
contract change that can affect actuation must never be combined with an
unrelated live-control change.

## Transitional mapping

Current `StrategyInputs`, `StrategyPlan` and `PlanLiveDirective` are production
models and remain supported while behavior is migrated. New code should target
the contracts package. Each phase replaces one adapter boundary, then deletes
the superseded transitional model only after live observation and rollback
criteria are satisfied.

# Scenario Builder Shadow and Cutover

Status: **Approved** on 2026-09-20 through the owner's instruction to prepare,
review, implement and make this shadow/cutover change deployment-ready. The
approved semantic name is `HOUSE_LOAD_COMPONENT`. Later semantic contract
changes still require a new impact analysis and owner approval.

## Decision

Make scenario generation a first-class pure module between forecasting and
optimization:

```text
Forecasting ---- ForecastDistributionBundle +
                                             +--> ScenarioBuilder --> ScenarioBundle
Feature Store -- ScenarioEvidenceSnapshot -+                           |
                                                                         v
                                                                   Optimization
```

The Scenario Builder is a computational module, not a control layer or Home
Assistant service. It has no actuator, price, battery-state or policy access.

This impact analysis records the approved semantic name
`HOUSE_LOAD_COMPONENT` for independently modeled loads. EV charging and PV
generation remain separate series because they have different physical and
policy semantics.

## Interface changes

### Forecasting output

`ForecastDistributionBundle` is marginal-only. It contains aligned load, PV
and EV forecasts; load keeps its stable `HOUSE_LOAD_COMPONENT` breakdown.
Scenario paths are not part of this bundle.

`EvForecast` is an explicit contract beside load and PV. It uses the same slot
grid and immutable forecast metadata. Every `EvForecastSlot` contains an energy
distribution, active probability, causal naive-climatology probability and
quality. The EV forecaster conditions an already active session on its captured
state; the Scenario Builder only couples that output with historical session
shapes. Optimizer EV policy never enters the EV forecast.

Device-specific inputs remain inside the owning forecaster:

- DHW temperature, hysteresis and lockouts remain private to the heat-pump
  forecaster;
- appliance activity and cycle progress remain private to the appliance
  forecaster;
- raw EV device/session state remains private to the EV forecaster. Normalized
  historical EV energy and session shapes may enter the Scenario Builder only
  through `ScenarioEvidenceSnapshot`.

The Scenario Builder receives forecast distributions, not those raw device
features. Total house load remains mandatory. Missing component forecasts do
not invalidate it; component series support analogue selection and diagnosis.

### Scenario evidence

`ScenarioEvidenceSnapshot` is deeply immutable and captured before scenario
building. Mappings are represented as sorted tuples of immutable records, not
mutable dictionaries hidden inside frozen dataclasses.
It contains only finalized, causal, slot-aligned observations and calibrated
distribution evidence:

- joint historical house-load-component, PV and EV energy;
- quality and completeness for every historical value;
- causal marginal residual samples keyed by series/model/lead-time cohort;
- timezone, normalized EV activity threshold and physical series bounds needed
  to validate paths.

It contains no Home Assistant entity IDs, database handles, mutable learning
state, prices, battery state or optimizer policy.

### Scenario builder

The public seam is:

```text
build(ScenarioBuildRequest) -> ScenarioBuildResult
```

`ScenarioBuildRequest` contains the marginal forecasts, evidence snapshot and
bounded generation settings. `ScenarioBuildResult` contains either one valid
`ScenarioBundle` or a structured unavailable status plus diagnostics. A valid
bundle has one grid, positive normalized weights, causal provenance and
coherent house-load/PV/EV paths. The builder may repair narrowly bounded gaps
under one documented quality policy; it may not invent forecast values or draw
series independently as a hidden fallback.

Marginal mapping prefers calibrated residual cohorts, then current emitted
P10/P50/P90 values, and finally a causal historical empirical distribution
centered on current P50. The final fallback derives uncertainty from captured
evidence rather than inventing a current forecast value. Source counts are
persisted by series and are part of release review.

The concrete contracts are:

```text
ForecastDistributionBundle
├── LoadForecast
│   └── HOUSE_LOAD_COMPONENT[]
├── PvForecast
└── EvForecast

ScenarioBuildRequest
├── forecast: ForecastDistributionBundle
├── evidence: ScenarioEvidenceSnapshot
└── settings: ScenarioGenerationSettings

ScenarioBuildResult
├── scenarios: ScenarioBundle | None
├── status: completed | insufficient_evidence | invalid_input | failed
└── diagnostics: ScenarioBuildDiagnostics
```

House-load components inform historical analogue selection and diagnostics.
Quality-valid component profiles weight candidate weeks by normalized distance;
missing component values are excluded rather than treated as zero.
The optimizer-facing path contains their validated aggregate
`house_load_kwh`; device-specific state never crosses into optimization.

### Historical gap policy

Gap handling is part of the versioned Scenario Builder implementation:

- missing or low-quality device components never reject an otherwise valid
  aggregate house-load/PV/EV path; that component is excluded from analogue
  similarity for the affected slot;
- current forecast values are never repaired;
- only isolated invalid continuous aggregate-load or PV rank-template slots
  bracketed by valid neighbours may be interpolated;
- a path may repair at most 2% of its slots and never consecutive slots;
- EV values and EV event boundaries are never interpolated;
- paths receive a weight penalty proportional to their repaired fraction,
  after which all positive weights are normalized;
- rejected paths, repaired slots, marginal source/fallback counts, excluded
  component values, policy version
  and reasons are recorded in diagnostics.

An eligible vintage is defined before scenario generation: the marginal bundle
is valid, evidence predates its generation time, and at least the configured
minimum number of historical weeks overlap the requested grid. Builder failure
therefore cannot remove a vintage from the availability denominator.

### Optimization input

`OptimizationProblem` receives marginal `forecast` and optional `scenarios` as
separate fields. The deterministic optimizer uses the marginal P50 forecast.
The stochastic optimizer requires a valid `ScenarioBundle`. Optimizers do not
read history, calibration state or scenario-generation diagnostics.

Fallback selection belongs to orchestration:

- valid scenarios: stochastic optimizer may be selected;
- unavailable scenarios: deterministic P50 optimizer with an explicit reason;
- invalid marginal forecast: planning follows the existing fail-closed policy.

## Shadow release

The first release keeps the current deterministic plan authoritative. After
that plan has been durably published, a failure-contained sidecar:

1. receives a deeply immutable `ScenarioBuildRequest` captured during the same
   planning snapshot, before publication or any learning-state maturation;
2. after authoritative publication, builds and validates `ScenarioBundle`;
3. runs the stochastic optimizer on the same market, battery and policy
   snapshot;
4. persists the marginal forecast, scenario result, authoritative plan,
   hypothetical plan and immutable optimization problem outside Recorder;
5. never reaches compiler, live control or actuation.

The trace schema is versioned. Retention remains 21 days and one trace per UTC
quarter-hour, with an additional bounded total-byte limit and oldest-day
eviction. Scenario diagnostics include readiness, rejected/repaired paths,
marginal-source counts, EV occurrence calibration error, runtime, evidence ID/cutoff, generator version,
seed, input fingerprint and model versions. It does not duplicate complete
history.

## Matured evaluation

The existing evaluator is extended rather than duplicated. Once actual slots
have finalized, it scores three independent questions:

1. **Marginals:** P50 error and calibrated interval coverage for house load,
   PV, EV and available house-load components.
2. **Scenarios:** CRPS, central-80% coverage, energy score, variogram score and
   EV occurrence/start/duration against finalized joint actuals. Scenario
   scores are compared with the degenerate P50 path.
3. **Decisions:** the shadow and authoritative first-slot permissions are
   replayed against the same full-horizon perfect-foresight problem. Perfect
   foresight replaces only load, PV and EV with finalized actuals over that
   historical horizon. It retains the market slots known at the vintage,
   captured SoC, battery constraints and policies. It evaluates only the first
   decision from the first plan captured at most 30 seconds after slot start;
   later-published prices and later battery state are never introduced. Report
   direction agreement, policy distance and paired monetary regret.

The degenerate comparison path uses emitted house-load, PV and EV P50 values,
not an always-inactive EV. A separate always-inactive EV baseline remains
visible. EV climatology is calculated causally at forecast time from preceding
history, stored in `EvForecastSlot`, and never recomputed from the complete
evaluation window.

Evaluation is read-only and never feeds forecasting, scenario generation,
optimization or live control.

## Pre-registered cutover gate

Review after at least seven complete UTC days measured from the first trace of
the new schema, not merely seven elapsed days:

- at least 90% of quarter-hour vintages on every counted day;
- at least 100 matured boundary decisions and 20 complete matured paths;
- scenario generation succeeds for at least 90% of eligible boundary vintages;
- every observed load/PV lead/regime cohort has at least 30 samples;
- central-80% coverage is between 65% and 95%;
- scenario CRPS, energy score and variogram score are no worse than P50;
- the upper 95% confidence bound of paired shadow-minus-authoritative
  perfect-foresight regret is at most zero;
- runtime remains below five seconds;
- no trace or scenario failure affects authoritative publication;
- EV scoring requires three matured sessions and 30 inactive slots. If this
  evidence is absent, EV remains explicitly inconclusive and requires a
  separate owner decision rather than being silently passed.

There is no automatic promotion.

Gate metrics use a fixed, predeclared sample: at most the first eligible
boundary vintage per UTC hour. Overlapping quarter-hour horizons are not
treated as independent evidence. Confidence intervals use UTC-day block
bootstrap. Sparse cohorts return `insufficient_data`, never a silent pass or
failure. Seven days are a minimum observation window, not a guaranteed cutover
date.

## Cutover release

The cutover branch is built from the same contracts, Scenario Builder,
stochastic optimizer and tests as the shadow branch. Its only behavioral
difference is orchestration: a valid `ScenarioBundle` selects the stochastic
optimizer before canonical plan publication. Explicit deterministic fallback
remains available for unavailable scenarios, complexity guard or optimizer
failure.

Compiler, live control, actuation and their contracts do not change. After
cutover, the shadow-only sidecar and parallel plan trace are deleted rather
than retained as a compatibility facade. Matured forecast/scenario/plan
evaluation remains as normal operational observability.

The prepared cutover implements that order without changing the approved
contracts. Scenario Builder executes once before `PlanningService`; a valid
bundle selects stochastic authority, while missing evidence, builder failure or
stochastic-optimizer failure selects the existing deterministic P50 plan with
an explicit diagnostic reason. Trace persistence receives the already-built
result and already-selected problem and cannot invoke either optimizer.

## Implementation sequence

1. Add contract tests for marginal-only forecasts, EV forecast, scenario
   requests/results and separate optimization input.
2. Extract scenario generation from `forecasting` into its own package with a
   README and agent rules.
3. Implement causal evidence capture, bounded gap repair and structured
   readiness diagnostics.
4. Migrate the shadow sidecar and version its trace schema.
5. Extend matured scenario and perfect-foresight evaluation and its tests.
6. Update architecture, interface contracts, layer READMEs and architecture
   dependency tests.
7. Run unit, architecture, evaluator, HACS and hassfest validation.
8. Apply the shared contract/module commits to the cutover branch and keep only
   the orchestration delta.
9. Run the combined Architecture/Home Assistant/Forecasting-Optimization
   critic. Resolve actionable findings before declaring the shadow deployable.
10. Version the integration and changelog, verify that slow shadow work cannot
    block a production refresh, and verify reload/unload invalidates pending
    background publication.
11. For deployment, back up the installed integration, run HA configuration
    validation, perform one confirmed restart, and verify that integration
    enablement and the existing discharge-at-load setting remain unchanged. No
    new entity or config option is introduced.

## Rollback and operational risk

The shadow release cannot alter the canonical plan or device command. Rollback
is the currently deployed RC27 commit `b92848a`; removing the new trace schema
does not alter planning state. The cutover is not deployed until the registered
evidence gate and owner review pass.

# Heat-pump production cutover and full-stack shadow

Status: **Approved** on 2026-10-10 by the owner's instruction to promote the
new heat-pump forecast, prepare a seven-day full-stack shadow and deploy the RC.

## Decision

Promote the evaluated space-heating point model into authoritative
forecast composition. Keep the established finite-cycle DHW model. Remove the
temporary whole-heat-pump sidecar rather than retaining a runtime selector or
compatibility facade.

The earlier aggregate promotion gate did not pass: although candidate MAE and
absolute bias improved, the upper bound of its day-block confidence interval
was `+0.00535 kWh/slot`. This is not relabelled as a pass. The owner explicitly
approved the staged production promotion after a segmented review showed the
targeted defect and benefit when space heating was active: MAE improved from
`0.1312` to `0.0720 kWh/slot` at 0-1 hour lead, from `0.1304` to `0.0836` at
1-6 hours and from `0.1016` to `0.0712` at 6-24 hours. At 24-48 hours it was
effectively neutral (`0.0767` versus `0.0773`). This recorded owner decision is
a one-time model-promotion exception, not a weakening of the later full-stack
cutover gate.

The deterministic optimizer remains authoritative. The existing
post-publication Scenario Builder and unified optimizer shadow now consume the
complete authoritative forecast bundle containing general house load, every
configured load component, the promoted heat-pump components, PV and EV. This
is the candidate stack evaluated for the later stochastic-optimizer cutover.

## Contract impact

No interface contract changes are required. `ForecastDistributionBundle`,
`ScenarioBuildRequest`, `ScenarioBundle` and `OptimizationProblem` already
carry the required marginal and scenario data. Units, signs, required fields
and invariants are unchanged.

## Uncertainty

The promoted point model receives a new component model version. Component and
total-load P10/P90 therefore use only matured residuals issued by the matching
production versions. Existing residuals from the replaced model are not
relabelled. Scenario generation uses its documented causal empirical fallback
while new cohorts mature.

## Seven-day observation and cutover gate

The evaluator selects one full implementation cohort by trace schema,
optimizer version, Scenario Builder version and load/PV/EV model versions.
Review begins after seven complete UTC days belonging to the RC34 cohort.

Cutover requires all pre-registered optimizer-shadow gates in
`docs/evaluation/README.md`, including trace coverage, scenario success,
runtime, uncertainty coverage, scenario scores and paired perfect-foresight
regret. Additionally:

- space-heating error is reviewed separately for active and inactive issuance
  states and by lead-time bucket;
- no unrelated load component or PV regression is accepted;
- current production planning must remain available throughout the window;
- missing evidence remains `insufficient_data`, never a pass.

There is no automatic promotion. If the gate passes, the cutover changes only
optimizer selection before canonical publication. Compiler, live control and
actuation remain unchanged. After cutover the parallel optimizer plan is
removed; bounded forecast/scenario/decision evaluation remains operational.

## Rollback

The pre-cutover recovery point is commit `65942d7`. Operational rollback
restores that integration version, validates Home Assistant configuration and
performs one controlled Home Assistant restart. Forecast traces are
non-authoritative and need not be deleted.

## RC34 critic follow-up

The post-deployment review found two release-evidence defects without changing
any public contract. `space-heating-v3` no longer clips measured active heating
to zero when historical active-power capacity is unavailable. Forecast traces
also persist the normalized active/inactive heating state at issuance, and the
offline evaluator reports authoritative heating error by that regime and lead
time. Aggregate load quality now reflects the weakest slot-specific component
quality. These fixes start a fresh exact-model observation cohort.

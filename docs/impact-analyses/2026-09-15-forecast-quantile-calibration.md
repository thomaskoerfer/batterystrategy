# Impact analysis: forecast quantile calibration

## Scope

The approved optional `QuantileEnergy.p10_kwh`, `p90_kwh` and
`calibration_samples` fields are populated when enough causal evidence exists.
No contract shape, unit, invariant or ownership changes.

## Method

Each production P50 is frozen in bounded forecast-owned learning state once per
target and lead-time class. It matures only after the exact target feature slot
is finalized. Residuals remain separated by series, point-model version and
lead-time class. Weekday/weekend and predicted active/inactive cohorts are used
after twelve residuals; otherwise progressively broader cohorts from the same
series, version and lead class are used. P10/P90 are empirical residual
quantiles anchored to the unchanged production P50.

Named components mature only against their own quality-valid actual. Both
missed and false-positive events remain in the unconditional error distribution.
Model-version separation starts fresh calibration after a point-model change.
Total load is calibrated directly against total EV-free load rather than by
summing component quantiles; PV is independent.

## Runtime and control impact

- Forecast P50 is unchanged.
- Optimization continues to consume P50 only.
- Compiler, live control and actuation are unchanged.
- Planning-state schema 11 gains additive bounded forecast-owned pending
  vintages and residual cohorts. Existing documents start both empty; canonical
  plan and execution-policy meanings do not change.
- No Recorder entity or Home Assistant configuration changes.
- Existing bounded forecast traces serialize quantiles for offline coverage and
  width evaluation but never feed calibration or control.

Quantiles initially remain absent. Broad near-term cohorts begin to appear after
twelve issued forecasts mature; long-lead and weekday/activity cohorts fill
later.
The emitted P50 calculations and optimizer parity are covered by regression
tests.

## Storage

Pending vintages are capped at 1,200 records and 32 named components plus the
two total series per record. Every residual cohort retains at most 96 values and
the least-recently-updated cohorts are capped at 1,024. Forecast traces remain
limited to one compressed vintage per quarter-hour, 192 slots, 32 components
and 21 days.

## Rollback

Reinstall `0.2.0-rc.25`. Quantile fields return to absent. Its schema-11 loader
preserves the additive calibration keys as unknown state without consuming them.

## Observation gate and step 2

The production observation window starts with deployment of this release. The
first review happens after seven complete local calendar days; for a deployment
on 2026-09-15 this is 2026-09-23. Components with too few matured events for a
meaningful assessment remain observational until day 14 rather than being
promoted on weak evidence.

The review evaluates total EV-free load, PV and every eligible separately
metered component independently and by lead-time class. It records:

- P50 MAE and bias to prove the unchanged point forecast has not regressed;
- P10/P90 empirical coverage, interval width and calibration sample count;
- missing or zero-width intervals and fallback-cohort usage;
- bounded-state size, update duration and any runtime errors.

Step 2 is an evidence review and an optimizer-policy proposal, not an automatic
control change. P50 remains the only optimizer input until the owner approves a
separate impact analysis for any use of uncertainty. The proposal must identify
which quantile or derived expectation is consumed, how it changes optimization,
which contracts are affected and how perfect-foresight and live regressions are
bounded. Sparse components may extend the observation window, but cannot delay
the independent total-load and PV assessment.

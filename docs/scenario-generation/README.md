# Scenario Generation

## Purpose

Scenario generation converts independent marginal forecasts into a bounded set
of weighted, coherent future paths. It preserves temporal and cross-series
dependence without moving forecast ownership into the optimizer.

It is a pure computational component within the forecasting-to-optimization
flow, not a Home Assistant service, control layer or hardware authority.

## Interface

```text
ScenarioBuilder.build(ScenarioBuildRequest) -> ScenarioBuildResult
```

The request contains one aligned marginal `ForecastDistributionBundle`, one
deeply immutable causal `ScenarioEvidenceSnapshot`, optional normalized market
observations and bounded settings. A
successful result contains a separate `ScenarioBundle`. An unavailable result
contains a structured status and diagnostics, never a fabricated path.

## Model

The current model selects complete matching historical weekly paths as the
joint rank template. It maps those ranks onto current marginals in this order:

1. calibrated residual cohorts;
2. emitted P10/P50/P90 forecast quantiles;
3. a causal historical distribution centered on current P50.

PV is capped by normalized inverter capability. EV remains separate from house
load and is never interpolated. Current active EV state may require an active
historical boundary; an inactive state still permits paths in which charging
starts later. Historical activity uses the normalized threshold captured with
the EV forecast evidence, not a greater-than-zero noise test or optimizer policy.

Short aggregate load/PV/price restart gaps bracketed by valid observations may
be interpolated, up to the versioned repair fraction. EV gaps are accepted only
when both boundaries imply the same active state; their energy comes from the
current EV distribution and is never interpolated. Missing device-component evidence does not reject a
quality-valid aggregate path. Repair and fallback counts are persisted for
evaluation; neither operation changes current marginal forecasts.
Available quality-valid house-load components rank and weight otherwise valid
weekly analogues by normalized profile distance. Missing component values are
excluded from that distance rather than interpreted as zero.

EV occurrence is assigned to the highest historical session ranks so each
slot's weighted event frequency tracks the `EvForecast` active probability to
the finite ensemble's weight resolution. Conditional active energy then maps
through the EV marginal distribution.

## Non-responsibilities

Scenario generation reads only normalized firm/proxy market observations to
preserve joint price dependence. It does not read providers, battery state, commercial policy,
Home Assistant, Recorder, files, networks or the wall clock. It does not choose
battery actions, compile budgets or issue commands. The optimizer must not
recreate scenario-generation behavior.

## Verification

Tests cover deterministic replay, grid alignment, normalized probabilities,
joint weekly path preservation, EV boundary treatment, aggregate repair limits,
component-quality isolation and structured insufficient-evidence results.
Offline evaluation compares marginal CRPS/coverage, energy and variogram scores,
and EV event/start/duration behavior against finalized actuals and P50 baselines.

## Setup Independence

The module consumes normalized contracts and semantic component keys only. It
must not contain entity IDs, device names, addresses, URLs, credentials or
installation-specific paths.

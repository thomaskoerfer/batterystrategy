# Forecasting, Scenario Generation and Stochastic Optimization

## Question

Should a probabilistic home-energy system use the explicit flow
`Forecaster -> Scenario Builder -> Optimizer`, rather than having forecasting
construct optimizer-ready scenarios internally?

## Conclusion

Yes. For Battery Strategy, scenario generation should be a first-class domain
module with a typed boundary. It does not need to be a separate process or Home
Assistant integration layer. The strongest design is:

```text
Feature data -> Marginal forecasters -> Scenario builder -> Stochastic optimizer
                     |                       |
                     v                       v
              marginal evaluation    joint-path evaluation
```

This split matches the statistical distinction between calibrated marginal
forecasts and a coherent joint distribution, and the optimization distinction
between uncertainty representation and decisions. It also localizes the
current historical-path completeness and repair policy in the component that
actually owns it.

## Evidence

### Marginal calibration and dependence are different responsibilities

Probabilistic forecasts should be sharp subject to calibration. Calibration is
a property of forecasts and observations and must be evaluated with proper
distributional diagnostics; it is not an optimizer concern
([Gneiting, Balabdaoui and Raftery, 2007](https://doi.org/10.1111/j.1467-9868.2007.00587.x)).

Independently calibrated quantiles do not define temporal or cross-series
trajectories. Ensemble Copula Coupling explicitly uses a staged procedure:
calibrate every univariate output, sample those predictive distributions, then
reorder the samples with an empirical copula to recover multivariate
dependence. The paper treats this as statistical post-processing after marginal
calibration
([Schefzik, Thorarinsdottir and Gneiting, 2013](https://arxiv.org/abs/1302.7149)).

The Schaake shuffle provides the same architectural evidence from another
method family: it leaves the forecast values unchanged and reorders ensemble
members using historical observations to recover temporal, inter-variable and
spatial dependence
([Clark et al., 2004](https://doi.org/10.1175/1525-7541%282004%29005%3C0243%3ATSSAMF%3E2.0.CO%3B2)).
Historical-path scenario generation has also been formulated directly for
multivariate wind and solar sequences used by stochastic programs
([Kaut, 2021](https://doi.org/10.1007/s10287-021-00399-4)).

These methods support a clean ownership rule: forecasters own calibrated
marginals; a scenario builder owns the dependence model and the finite weighted
sample presented to optimization.

### Stochastic MPC consumes scenarios; it should not silently manufacture them

Scenario-based MPC evaluates common near-term actions over multiple future
disturbance paths. Building-energy research models those paths as realizations
of uncertain disturbances and then passes them to the MPC problem
([Pippia et al., 2019](https://repository.tudelft.nl/file/File_ec35ef0e-8438-4a89-af72-b627e822bc69)).
A recent operational framework makes the stages explicit: probabilistic
forecasting, dependence-aware scenario generation and reduction, then
stochastic optimal control
([van der Heijden et al., 2025](https://doi.org/10.1029/2024WR037115)).

The boundary is also visible in practical energy software. PyPSA accepts named
scenarios and probabilities, enforces common first-stage decisions, and solves
weighted recourse; callers define the scenario data before invoking the
optimizer
([PyPSA stochastic optimization documentation](https://docs.pypsa.org/latest/user-guide/optimization/stochastic/)).
EMHASS separates forecasting from its deterministic optimization core, but it
does not provide an equivalent multivariate scenario-generation boundary; it
is therefore a useful deterministic baseline, not evidence that quantile
coupling belongs in the optimizer
([EMHASS repository architecture](https://github.com/davidusb-geek/emhass/blob/master/AGENTS.md)).

Some probabilistic models directly learn and sample a joint trajectory
distribution, for example normalizing-flow energy forecasts
([Dumas et al., 2021](https://arxiv.org/abs/2106.09370)). That does not remove
the boundary: an adapter still has to validate grids and physical bounds,
assign or normalize weights, reduce the sample to the computational budget and
record provenance before optimization. It merely changes the scenario
builder's source from empirical copula coupling to native joint samples.

## Recommended ownership

### Forecasters

Own:

- P10/P50/P90 or richer predictive marginals for EV-free load, PV and EV;
- marginal calibration, clipping to variable-specific physical bounds and
  model/training provenance;
- missingness and quality for each forecast variable and lead time.

Do not own:

- coupling ranks across time or between load, PV and EV;
- scenario probabilities, scenario reduction or optimizer risk preferences.

### Scenario builder

Own:

- temporal and cross-series dependence;
- choice of ECC, Schaake-style historical templates, native joint samples or a
  future copula/generative method;
- path selection, narrowly bounded gap repair, physical joint validation,
  scenario reduction and normalized probabilities;
- diagnostics describing available history, repaired/rejected paths,
  calibration sufficiency and generation method;
- deterministic reproducibility from an explicit generation timestamp and
  seed/version.

It must not know prices, battery SoC, battery limits, commercial policy or live
measurements beyond the forecast inputs required to condition scenarios.

### Optimizer

Own:

- battery physics, market valuation and policy constraints;
- non-anticipativity/common executable decisions and scenario-dependent
  recourse;
- expected-cost, CVaR or other explicitly configured risk objectives;
- optimization diagnostics and the resulting canonical battery plan.

It consumes scenarios as facts about uncertainty. It must not repair history,
calibrate quantiles, infer dependence or silently invent missing scenarios.

## Suggested contracts

The eventual architecture should separate the current combined bundle into two
logical contracts:

1. `ForecastDistributionBundle`
   - common slot grid and generation time;
   - per-variable P50 and calibrated marginal quantiles/distributions;
   - training cutoff, model version and quality metadata.
2. `ScenarioBundle`
   - the same exact slot grid;
   - bounded coherent load/PV/EV paths and positive normalized weights;
   - scenario-builder method/version, generation time, source forecast identity,
     quality summary and reproducibility metadata.

The optimizer should accept an `OptimizationProblem` containing either a valid
`ScenarioBundle` or an explicitly selected deterministic forecast. It should
not receive both and decide implicitly which is trustworthy.

This is a semantic contract change from the current optional
`ForecastBundle.scenarios` field. It therefore requires a separate impact
analysis and owner approval before implementation. No contract change is made
by this research note.

## Fallback behavior

Fallback must be explicit and observable:

- Valid scenario bundle: run stochastic optimization.
- Marginals valid but scenario generation unavailable or insufficient: run the
  established deterministic P50 optimizer and report a structured degradation
  reason.
- Forecast itself invalid: fail planning closed under the existing planning
  policy; the scenario builder must not fabricate a forecast.
- A few repairable historical gaps: repair only under the scenario builder's
  documented quality policy and expose counts/provenance.
- Dependence unavailable: do not sample load, PV and EV independently as an
  unnoticed fallback. Independent draws can destroy persistence and
  coincident extremes, precisely the properties ECC and the Schaake shuffle
  are designed to restore.

The deterministic fallback belongs in orchestration/planning policy, not in
the optimizer implementation. This keeps both optimizers pure and makes the
degraded mode testable.

## Decision for Battery Strategy

Adopt `Forecaster -> Scenario Builder -> Optimizer` as the target architecture.
Treat the Scenario Builder as a sixth computational component but not a sixth
control layer: it transforms forecast uncertainty and has no authority over
plans or hardware. Forecast evaluation remains marginal; scenario evaluation
adds multivariate/path metrics and downstream decision value; optimizer
evaluation remains economic against matured actuals and perfect foresight.

Before changing code, prepare an impact analysis for extracting
`ForecastBundle.scenarios` into `ScenarioBundle`, including migration of both
the shadow and cutover branches. The current shadow issue should then be fixed
inside that component rather than by adding more historical-path policy to the
forecaster or optimizer.

# Coherent forecast scenarios and stochastic optimizer

Status: owner-approved on 2026-09-19 for implementation as a replacement
shadow release. The previously proposed three-quantile-envelope gate is
superseded by this analysis.

## Decision

The forecast contract is extended additively with an optional, weighted set of
coherent load/PV/EV paths. Marginal P10/P50/P90 values remain available for
calibration diagnostics, but are not treated as complete future trajectories.

The first release keeps the existing deterministic P50 optimizer authoritative
and executes the stochastic optimizer after it, failure-contained. This new
shadow replaces the RC26 quantile-only observation gate. RC26 remains only the
known-good production rollback point until cutover.

After the shadow comparison is accepted, the prepared cutover branch makes the
stochastic optimizer authoritative when valid scenarios are present and falls
back to deterministic P50 when they are absent. Compiler, live-control and
actuation contracts do not change.

## Contract impact

`ForecastBundle` gains optional `scenarios`. Every scenario:

- uses exactly the bundle slot grid;
- carries EV-free house load, PV generation and EV charging separately;
- has a positive probability and non-negative slot energies;
- belongs to a set whose probabilities sum to one;
- records generation and training cut-off timestamps.

`OptimizationProblem` gains an explicit EV interaction policy. This prevents
the optimizer from inferring live-control semantics. EV remains excluded from
house load and is never hidden inside its quantiles.

The owner approved these additive contract changes and their semantics in the
conversation on 2026-09-19. Producer, optimizer and integration tests must move
together. Persisted schemas are unchanged because scenario and shadow data are
not part of the executable-plan snapshot.

## Forecast implementation

The first scenario model is an empirical weekly-path ensemble:

1. Complete historical paths from prior matching weekdays are selected using
   only finalized data available at the forecast cut-off.
2. For load and PV, each historical path contributes its deviation from the
   median historical path to the current P50 forecast. This preserves the
   current weather/component point forecast while retaining historical serial
   dependence.
3. EV paths are carried separately from the same historical weeks. An active
   current EV measurement anchors only the first slot.
4. A path is emitted only when all requested slots are present and quality
   valid. All retained paths receive equal probability.

Selecting the same historical week jointly for load, PV and EV is a
Schaake-shuffle-style empirical copula: temporal and cross-series dependence is
preserved instead of independently sampling marginal quantiles. The model is
setup-neutral and bounded to 12 scenarios.

## Optimizer implementation

The stochastic optimizer is a receding-horizon, two-stage model:

- the first battery transition is common to every scenario
  (non-anticipativity);
- future actions are scenario-specific recourse;
- the common action minimizes probability-weighted import/export cost and the
  existing cycling margin;
- the visible remainder is a deterministic P50 recourse plan constrained to
  that common first transition and is recalculated at the next planning run;
- risk is initially expected cost. CVaR or other aversion is a later, measured
  policy change, not a hard-coded safety premium.

EV allocation follows the existing policy switches: PV-to-EV priority,
discharge while EV charging, and whether battery energy may serve EV demand.

## Shadow and evaluation

The shadow release publishes the existing deterministic plan unchanged. It
records bounded diagnostics for scenario readiness, stochastic first action,
P50 first action, disagreement and expected scenario cost. Failure or timeout
in scenario generation or shadow optimization cannot delay or alter the
authoritative plan.

Release evaluation compares both plans on identical vintages against matured
actuals and perfect foresight. Required checks are action agreement, realized
cost/regret, PV export, grid charging, EV collision, scenario coverage and
runtime. The comparison is observational and cannot feed planning.

## Rollback

- Shadow: disable/remove the optional scenario generation and shadow call;
  authoritative behavior is already RC26-equivalent.
- Cutover: restore the shadow release or RC26. A changed optimizer version
  invalidates stale executable plans through the existing plan lifecycle.

## Public-method basis

The design follows scenario-based stochastic MPC with a common first action,
empirical dependence reconstruction (Schaake shuffle / ensemble copula
coupling), and rolling re-optimization. Public household optimizers reviewed
for comparison use deterministic point forecasts; they are useful baselines,
not evidence that marginal quantiles form valid trajectories.

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
house load and is never hidden inside its quantiles. The policy includes the
same configurable EV-active threshold used by live control; values below it do
not trigger EV-specific discharge restrictions.

The owner approved these additive contract changes and their semantics in the
conversation on 2026-09-19. Producer, optimizer and integration tests must move
together. The non-authoritative trace moves to schema 2. Executable-plan
persistence gains an internal optimizer-generation marker so a deployment or
rollback cannot execute intent created by a different optimizer generation.

## Forecast implementation

The first scenario model is an empirical weekly-path ensemble:

1. Complete historical paths from prior matching weekdays are selected using
   only finalized data available at the forecast cut-off.
2. For load and PV, each historical path contributes a joint rank template.
   The rank selects from the matching current point-model/lead-time residual
   distribution around P50; PV remains capped by inverter power.
3. EV paths are carried separately from the same historical weeks. Current
   active/inactive state selects compatible sessions and an active measurement
   anchors the first slot.
4. A path is emitted only when all requested slots are present and quality
   valid. All retained paths receive equal probability.

Selecting the same historical week jointly for load, PV and EV is a
Schaake-shuffle-style empirical copula: calibrated marginals are coupled through
historical temporal and cross-series ranks. The model is setup-neutral and
bounded to 12 scenarios. Missing residual calibration disables scenarios and
therefore keeps the deterministic P50 path authoritative.

## Optimizer implementation

The stochastic optimizer is a receding-horizon, two-stage model:

- the first battery transition is common to every scenario
  (non-anticipativity);
- future actions are scenario-specific recourse;
- the common action minimizes probability-weighted import/export cost and the
  existing cycling margin;
- the visible remainder is a deterministic P50 recourse plan constrained to
  that common first transition and is recalculated at the next planning run;
- the executable first-slot discharge budget is capped to that common action;
- risk is initially expected cost. CVaR or other aversion is a later, measured
  policy change, not a hard-coded safety premium.

EV allocation follows the existing policy switches: PV-to-EV priority,
discharge while EV charging, whether battery energy may serve EV demand and the
configured active threshold. If battery priority permits PV to be diverted from
an active EV, the optimizer values that diverted energy as induced grid import,
not as free PV.

## Shadow and evaluation

The shadow release publishes and persists the existing deterministic plan
before scenario generation starts. A best-effort evaluation task then builds
the scenarios, runs the stochastic optimizer and records bounded diagnostics
for readiness, both first actions, expected scenario cost and runtime. Failure
or slow execution cannot delay or alter the authoritative plan; a busy sidecar
drops later observational vintages instead of queueing work.

Release evaluation compares both plans on identical vintages against matured
actuals and perfect foresight. The first executable actions are compared with a
perfect-foresight replay; scenario CRPS, P10-P90 coverage, EV-event Brier score
and runtime are reported separately. The comparison is observational and
cannot feed planning.

## Rollback

- Shadow: disable/remove the optional scenario generation and shadow call;
  authoritative behavior is already RC26-equivalent.
- Cutover: restore the shadow release or RC26. The optimizer-generation marker
  invalidates stale executable plans in either direction.

## Public-method basis

The design follows scenario-based stochastic MPC with a common first action,
empirical dependence reconstruction (Schaake shuffle / ensemble copula
coupling), and rolling re-optimization. Public household optimizers reviewed
for comparison use deterministic point forecasts; they are useful baselines,
not evidence that marginal quantiles form valid trajectories.

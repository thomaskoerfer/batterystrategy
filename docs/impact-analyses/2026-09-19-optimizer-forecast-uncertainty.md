# Proposed impact analysis: optimizer use of forecast uncertainty

Status: **Proposed**. This document is not owner approval and does not authorize
production behavior changes.

## Reason and evidence

Forecasting now emits empirically calibrated P10/P50/P90 slot energy when enough
causal evidence exists. Optimization still consumes P50 only. Before uncertainty
can affect control, the candidate behavior must be compared causally with the
current optimizer and perfect foresight.

Load and PV quantiles are marginal, slot-local estimates. They do not define a
joint probability distribution across series or time. Their combinations are
therefore conservative stress envelopes, not probabilistic P10/P90 net-load
scenarios. The optimizer must not derive an expected value from arbitrary fixed
weights or describe an envelope as a likelihood.

## Stage A: non-authoritative evidence

Production planning remains unchanged. Evaluation derives three aligned stress
envelopes for replay:

- central: load P50 and PV P50;
- scarcity envelope: load P90 and PV P10;
- surplus envelope: load P10 and PV P90.

A non-central envelope is used for a slot only when load and PV both provide a
complete calibrated pair. Otherwise that complete slot falls back to P50 for
both series. Forecasting remains the sole owner of calibration readiness;
evaluation and optimization do not add a second sample threshold.

The scenario adapter is evaluation-only. It creates contract-valid synthetic
totals without named components and assigns distinct non-authoritative forecast,
problem and plan identities. It does not weaken `ForecastBundle` invariants.

### Reproducible input capture

One bounded, versioned, compressed evaluation snapshot is retained per planning
quarter-hour. It contains the exact contemporaneous `OptimizationProblem`, the
authoritative P50 `BatteryPlan`, policy fingerprint and software versions needed
for deterministic replay. It contains no Home Assistant objects, entity IDs,
credentials, vendor payloads or actuator reference.

The capture is non-authoritative and failure-contained. Writing or evaluating it
cannot delay planning, change persisted canonical plans, reach the compiler or
authorize hardware. Retention is 21 days and publication is atomic.

### Evaluation

The existing pure optimizer runs independently against the central, scarcity
and surplus envelopes. Later finalized actual slots are joined by exact UTC slot
key. Reports keep metrics separate rather than hiding trade-offs in one score:

- realized cost and regret against perfect foresight;
- avoidable high-price import and PV export;
- planned grid charge and battery throughput;
- end-SoC error;
- charge, discharge and budget divergence from the authoritative P50 plan;
- quantile availability by series, slot and lead-time bucket.

Replay must define its physical clipping and rolling-replan assumptions
explicitly. It must not reconstruct old inputs from current settings or use
information unavailable at the recorded planning timestamp.

## Stage B: production decision after evidence

No production algorithm is approved in Stage A. In particular, the proposal no
longer claims that scarcity can protect inventory while leaving every P50 action
unchanged: the plan invariant requires discharge budget to cover planned
discharge. A production scarcity policy that protects energy may therefore need
to change planned discharge and expected SoC, not only discretionary budget.

The evidence review will compare two deliberately distinct candidates:

1. budget-only: tails may alter optional discharge permission above the P50
   action; this cannot protect energy already consumed by the P50 trajectory;
2. trajectory-aware: uncertainty may alter planned actions, budgets and expected
   SoC inside the optimizer while keeping `BatteryPlan` and downstream contracts
   unchanged.

The review must define exact equations and precedence before either candidate is
approved. It must cover partial quantile availability, conflicting scarcity and
surplus envelopes, price ordering, replacement energy, lookahead, terminal
value and horizon boundaries.

PV recovery remains on its existing P50 confidence-and-reserve formula during
Stage A. A future proposal must explicitly replace or retain that formula; P90
surplus must not be multiplied by the existing confidence coefficient without a
new, defensible meaning. Grid-charge uncertainty is also deferred because
marginal quantiles alone do not establish expected charging cost.

## Contract impact

Stage A changes no executable contract or production decision. It adds a
versioned evaluation-storage schema and a non-authoritative adapter.

Stage B will change optimizer semantics because optional forecast fields begin
to affect `BatteryPlan`. The existing contract shape, units and ownership can
remain unchanged, but the semantic change still requires a separate approved
impact analysis. Quantiles remain confined to evaluation and optimization;
compiler, live-control and actuator contracts must not receive them.

## Observability, restart and rollback

Stage A exposes only non-authoritative report output. A later production
candidate must provide optimizer diagnostics showing per-slot joint quantile
availability, selected uncertainty policy, P50 counterfactual and the source of
every budget difference.

The candidate must use a new optimizer version and execution-policy fingerprint.
Rollback must invalidate a persisted candidate plan before control resumes and
must handle an active compiler commitment explicitly. Merely installing older
files is insufficient because canonical plans and active-slot commitments are
persisted.

## Verification and gates

Stage A implementation requires:

1. contract-valid stress-envelope construction and exact P50 fallback tests;
2. bounded, atomic, redacted snapshot persistence and failure containment;
3. deterministic replay from the captured problem without current configuration;
4. proof that production `BatteryPlan`, compiler directive and live command are
   bit-for-bit unchanged when capture and evaluation are enabled;
5. architecture, HACS, restart and full regression checks.

Evidence review starts after at least seven complete local days. Each evaluated
lead bucket must have at least 100 jointly quantile-covered load/PV slots, and
the 80% interval target must lie inside the empirical 95% Wilson confidence
interval for both total load and PV. Sparse buckets remain observational rather
than being pooled silently.

Candidate comparisons use paired planning vintages and report confidence
intervals for each metric. Promotion requires no statistically supported
regression in realized cost, high-price import, PV export, grid charge or
throughput; any unresolved trade-off returns to the owner instead of being
collapsed into a composite score.

## Rollout

Development stays on `codex/optimizer-quantiles`. Stage A may be implemented and
tested locally after owner approval, but nothing from this branch is deployed
before the current forecast observation gate is accepted. Stage B requires a
second explicit approval after its exact semantics and replay evidence exist.

## Owner decisions required now

1. Approve or reject Stage A: bounded exact optimizer-input capture plus the
   three non-authoritative stress-envelope replays.
2. Confirm whether a later uncertainty-aware optimizer may change the canonical
   action and SoC trajectory when evidence supports it. Without that permission,
   scarcity handling is necessarily limited to optional budget above P50 actions.

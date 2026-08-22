# Impact analysis: economic discharge replacement

## Reason and evidence

On 2026-08-22 the authoritative optimizer planned discharge at 18.56 and
18.31 ct/kWh immediately before planned grid charging at 18.20 ct/kWh. With
85% round-trip efficiency and the configured 1 ct/kWh minimum margin, later
grid input costs at least 22.41 ct per deliverable kWh. The plan therefore
created a negative-value discharge/recharge cycle.

The live controller correctly kept EV load excluded and consumed only the
explicit slot budget, but this did not make the commercial permission valid.
The defect was inside the optimizer objective: a historical cheap-charge credit
subtracted a rank-based fictional rebate from real grid cost, while the average
acquisition cost of existing inventory lowered the discharge floor. Together
they made replacement appear cheaper than it was.

## Approved semantics

The owner approved the proposal and implementation scope on 2026-08-22.
Optimizer step cost consists only of real grid-import cost, foregone export
revenue and the configured discharge margin. Price ranks and cheap-window
heuristics cannot create monetary credits. Round-trip loss remains represented
by the battery state transition.

Historical inventory cost remains savings-accounting information. It does not
lower the optimizer's forward-looking replacement-cost floor. A future grid
charge releases current inventory reserved for later higher-value household
load only when the current avoided import covers the future input price divided
by round-trip efficiency plus minimum margin. Forecast PV replacement uses its
export opportunity cost and retains the existing confidence discount.

The conservative cheapest-credible-replacement floor remains a feasibility
guard against horizon-value artifacts. The monetary objective independently
prices real flows and margin; neither mechanism may use a fictional charge
credit.

## Contract and dependency impact

The `BatteryPlan` schema, slot units, signs and plan/live boundaries are
unchanged. This narrows the existing commercial-permission semantics of
`discharge_budget_kwh`; some low-price budgets and planned discharge actions
become zero. Forecasting, plan compilation, EV/PV live policy, slot budget
consumption and actuation are unchanged.

Optimized and baseline cost reporting now includes configured export revenue.
The user-selected minimum margin remains a policy guard and is not reported as
an electricity payment. Historical inventory cost diagnostics are renamed as
accounting diagnostics so they cannot be mistaken for an optimizer threshold.

## Verification

The incident replay fixes SoC at 15%, minimum SoC at 5%, capacity at 6 kWh,
round-trip efficiency at 85%, margin at 1 ct/kWh, current prices at 18.56 and
18.31 ct/kWh and later grid charging at 18.20 ct/kWh. Both early slots must
have zero planned discharge and zero discharge budget while the later economic
charge plan remains available.

Unit coverage proves the exact replacement break-even, the real-flow step-cost
calculation, profitable firm grid replacement, non-economic grid replacement,
quantitative PV replacement and all existing scarcity, spill-recovery,
plan-budget, EV and live-budget invariants. The complete test, HACS and Hassfest
suites run before release. A retained-trace comparison reviews plan cost,
battery throughput, budget openings, minimum/terminal SoC and missed expensive
load before cutover.

## Rollout and rollback

This optimizer correction is included in the not-yet-deployed
`0.2.0-beta.20` candidate and remains isolated from its Phase-3 shadow forecast
work. Deployment requires an announced Home Assistant restart, an exact backup
of the integration files being replaced and post-restart command/plan review.
The prior deployed integration release remains the rollback point; restoring it
and restarting Home Assistant requires no data migration. Phase-3 feature-store
schema rollback follows its separate impact analysis.

## Status

- Proposed: 2026-08-22
- Approved by owner: 2026-08-22
- Implemented locally: 2026-08-22
- Observed: pending deployment and live plan review

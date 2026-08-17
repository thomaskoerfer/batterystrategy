# Optimizer mode penalty and discharge-plan consistency

## Reason and evidence

On 2026-08-17 the live controller correctly discharged against an explicit
commercial budget while the optimizer profile stayed idle. A minimized replay
with scarce inventory and two similar price peaks reproduced the defect: the
highest current slot received `0.000 kWh` planned discharge while a cheaper
slot was used to avoid a modeled stop/start sequence.

The optimizer assigned fictional energy costs to plan-mode transitions. Plan
modes are economic intent, not actuator state: real PV surplus may legitimately
turn a planned discharge slot into live charge-follow. Transition costs in the
economic objective therefore penalize both ordinary idle boundaries and valid
PV overrides.

## Semantic impact

Artificial plan-mode transition costs are removed from the 15-minute economic
objective. RTE, minimum margin and micro-cycle suppression continue to reject
unprofitable charge/discharge cycles. Live command smoothing, PV override and
actuator write throttling remain unchanged.

`BatteryPlanSlot` is tightened so planned discharge cannot exceed its explicit
commercial discharge budget. A budget may remain larger than planned discharge
to cover unexpected live household load.

## Dependency and decision impact

Only the legacy optimizer mathematics and the target plan contract change. The
optimizer may move scarce discharge energy toward objectively higher-price
slots, and its expected SoC profile may therefore change. Forecasting, plan
compilation, live meter following, EV/PV policy and actuation are unchanged.
Dashboards consume the corrected plan without a schema change.

## Compatibility

No persisted schema or configuration changes. Tightening the in-memory plan
invariant is intentional: new optimizers must not emit planned discharge that
the plan compiler is commercially forbidden to execute. Existing callers that
construct such an invalid target-contract plan fail fast.

## Verification

- A regression scenario covers two similar price peaks separated by cheap slots
  and scarce inventory.
- Contract tests cover the planned-discharge budget invariant.
- Existing PV headroom, future-price reservation, EV, live-budget and actuator
  tests remain green.
- The retained-history backtest and live profile are reviewed before and after
  deployment.

## Rollout and rollback

The change is isolated from the forecast shadow and live controller. Rollback
is the Git tag `pre-optimizer-operating-mode-20260817`. Deployment preserves
the active options and is followed by a forced optimizer refresh and command
health verification.

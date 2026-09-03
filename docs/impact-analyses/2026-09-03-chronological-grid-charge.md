# Impact analysis: chronological grid-charge substitution

## Defect

The optimizer rejected current grid-charge transitions when aggregate cheaper
charging capacity existed anywhere later in the horizon. That capacity could
occur after the expensive demand the charge was intended to serve, so the
shortcut violated causality even though the dynamic-programming state itself
was chronological.

A second post-optimization filter guessed that discharge shortly after paid
charging belonged to that charging cycle. It could remove discharge without
removing or reallocating the corresponding charge, stranding paid inventory and
making the published plan more expensive than the optimized path.

## Decision

Both shortcuts are removed. The dynamic program already models slot order,
battery state, power and energy limits, RTE, import and export prices, minimum
margin and terminal value. Removing transition pruning expands the feasible
set without changing the objective. Uneconomic cycles remain rejected by their
real cost plus the configured discharge margin. Equal-cost charging remains as
late as feasible through the existing deterministic tie-break.

## Contract impact

There is no schema or interface-contract change. `OptimizationProblem` and
`BatteryPlan` retain their approved meanings. Forecasting, plan compilation,
live control, EV policy, discharge-budget consumption and actuation are not
changed.

## Verification

- A cheaper slot after expensive demand cannot replace charging needed before
  that demand.
- A cheaper slot before expensive demand still receives the charge.
- Insufficient cheaper capacity before demand retains the required earlier
  charge.
- Existing RTE, margin, PV, terminal-value, budget and deterministic-tie tests
  remain green.
- Perfect-foresight replay over all complete retained 24-hour windows must never
  increase modeled cost against the previous optimizer.

## Rollout

Deploy only after the current live-horizon replay and retained-history replay
are reviewed, all repository checks pass and the Home Assistant restart is
explicitly coordinated. Rollback restores the preceding integration files; no
configuration or persistence migration is involved.

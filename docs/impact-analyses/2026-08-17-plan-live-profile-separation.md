# Plan and live profile separation

## Reason and evidence

The published future profile was copied and then modified with the current live
command. The command was written into the next complete 15-minute point and its
energy difference was added as a constant percentage shift to all later SoC
values. Later planned power and discharge budgets were not re-optimized. Near
the minimum SoC this could display discharge while the shifted SoC was already
clamped to the configured floor.

## Contract impact

Published future profiles are now explicitly canonical `BatteryPlan` output.
Measured history is merged only up to the current timestamp. Live directives and
commands remain separate observability data and cannot mutate future plan SoC,
power, grid forecast or budget values.

This tightens the optimization output contract without changing its schema. Any
future proposal to publish a live-adjusted projection requires a separate named
projection, sequential energy replay and an impact analysis; it must not replace
the canonical plan.

## Runtime impact

The change affects dashboard and diagnostic profile publication only. Economic
optimization, forecast generation, plan compilation, live meter following,
PV/EV policy, budget consumption and hardware actuation are unchanged. A live
deviation is incorporated naturally when the next optimizer run reads the real
battery SoC.

## Verification and rollback

A regression test joins a divergent current actual point with unchanged today
and tomorrow plan points and verifies SoC, power and discharge budget. The full
test and HACS validation suites run before deployment. Rollback is the Git tag
`pre-plan-live-profile-separation-20260817`.

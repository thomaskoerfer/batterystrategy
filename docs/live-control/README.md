# Live control policy

This document defines the operator-visible behavior of Battery Strategy's live
control layer. It is normative together with `INTERFACE_CONTRACTS.md` and the
executable contracts in `custom_components/battery_strategy/contracts`.

The behavior below was approved by the repository owner on 2026-08-31. Changing
precedence, EV treatment, manual override semantics or disabled-control writes
requires an impact analysis and renewed explicit approval.

## Responsibility

Live control combines one valid `PlanLiveDirective` with normalized grid, PV,
battery, EV and SoC measurements. It follows the meter at the fast coordinator
cadence. It does not read prices, construct forecasts or optimize future slots.

Operator choices are passed as a typed `LivePolicy`; the pure controller does
not read Home Assistant options itself.

## Decision precedence

The first applicable rule wins:

1. control disabled by orchestration;
2. explicit manual charge or discharge;
3. required planned charge (`must_charge`);
4. live PV-surplus follow;
5. selected automatic discharge mode;
6. idle.

Independent BMS limits, stale-input fail-safe, configured SoC limits and
physical power limits constrain every active command.

## Automatic discharge modes

### Off

Automatic discharge is blocked. Planned charge and PV-follow remain available
when their own policies permit them.

### Load following

The battery follows eligible household load without using an optimizer
discharge budget. It remains bounded by live load, minimum SoC, maximum power
and EV policy. It must not deliberately export battery energy.

### Price sensitive

The battery follows eligible household load only while the active slot has
remaining latched commercial discharge budget. Budget is permission, not a
power target. Live power still follows measured load and remains export-safe.

## PV-follow

Automatic PV-follow reacts to measured surplus rather than forecast surplus.
It may operate while the economic plan is idle, blocked or prepared to
discharge. Required grid charging has higher precedence but may use live PV as
part of the required total.

With `PV first to EV` enabled, battery charging uses only surplus remaining
after actual EV consumption. With it disabled, EV power is removed from the
surplus calculation so the battery may compete with the EV and equivalent grid
import is an accepted consequence.

## EV discharge policy

The two EV discharge controls are independent:

| Discharge during EV charging | Battery may feed EV | Automatic behavior |
| --- | --- | --- |
| Off | Either | No automatic discharge while EV charging is active |
| On | Off | EV power is removed; only remaining household load is eligible |
| On | On | Total eligible load, including EV, may be supplied |

The configured EV active threshold determines when these rules apply. If an EV
sensor is configured but remains stale beyond its bounded bridge, automatic
discharge fails closed whenever correct EV exclusion cannot be guaranteed.

These EV rules apply identically to load-following and price-sensitive automatic
discharge.

## Manual override

An explicit manual charge or discharge command overrides automatic plan,
budget, PV and EV policy for its configured duration. It still obeys SoC,
maximum power, measurement freshness and actuator safety.

Manual discharge is a requested battery power, not meter following. It may
therefore supply an EV or export to the grid. This is intentional and must not
be silently converted into an automatic load-following command.

## Control disabled

`strategy_enabled` belongs to orchestration around the pure live controller.
When control changes from enabled to disabled, Battery Strategy writes safe zero
input and output limits once. After that succeeds it performs no further
hardware writes while disabled, allowing external manual battery operation.

Re-enabling control returns authority to the single Battery Strategy actuator
path. There is never a second automatic hardware writer.

## Direction and safety

- Charging and discharging are separate positive flows in measurements.
- Battery influence is removed before reconstructing eligible household load.
- Direction changes zero the opposite hardware limit before applying a new one.
- Minimum command and delta thresholds reduce oscillation and unnecessary
  writes without changing commercial permission.
- An invalid or expired directive, stale grid input or stale SoC produces a safe
  idle/zero outcome.

## Transitional implementation

The current production behavior already implements the precedence, EV policy,
manual override, one-shot disabled zero and price-sensitive slot-budget latch.
Before the Phase-6 cutover, some ownership remains transitional:

- operator modes are supplied through `StrategyOptions` instead of the target
  `LivePolicy` contract;
- load-following uses a synthetic maximum slot budget internally even though
  its observable behavior is budget-independent;
- required charge is not yet latched through the same explicit compiler state;
- slot commitment state is coordinator-owned rather than a pure compiler input
  and output.

Phase 6 removes these structural differences under shadow parity. It is not an
authorization to alter the behavior documented above.

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
not read Home Assistant options itself. Its only output is `LiveControlResult`:
one actuator-ready command, next explicit control state and diagnostics.

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
- Direction hysteresis state is explicit in `LiveControlState`; it is not held
  in the coordinator or actuator.
- Minimum command and delta thresholds reduce oscillation and unnecessary
  writes without changing commercial permission.
- An invalid or expired directive, stale grid input or stale SoC produces a safe
  idle/zero outcome.

## Setup independence

Live control consumes normalized measurements, policy and directives. It must
not depend on installation-specific entity IDs, names, addresses, hostnames,
URLs, serial numbers or provider payloads. Device command translation belongs
to the actuator boundary.

## Verification

Regression scenarios cover decision precedence, all EV-policy combinations,
PV-follow, required charge, both automatic discharge modes, manual override,
stale inputs, command smoothing and one-shot disabled control. Trace comparison
must separate economic permission from live meter-following behavior.

## Current implementation

The deterministic plan compiler is the sole owner of slot commitment state and
the sole producer of the contract live directive. The Home Assistant adapter
maps operator options once into `LivePolicy`. Load-following remains
budget-independent, while price-sensitive discharge uses the slot budget and
consumed-energy progress. Required charging and within-slot replanning use the
same explicit compiler state. No coordinator-owned latch, duplicate directive,
diagnostic command or alternative compiler remains.

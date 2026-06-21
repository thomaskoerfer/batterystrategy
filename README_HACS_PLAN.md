# Battery Strategy HACS Migration Plan

This repository now contains an initial HACS-style custom integration skeleton in
`custom_components/battery_strategy`.

The first implementation target is deliberately narrower than the legacy package:

- no grid charging
- PV surplus charging can be enabled
- discharge mode `Immer bei Last`
- manual charge/discharge as explicit overrides
- EV policy applies only to automatic strategy, not manual overrides

The legacy Home Assistant package remains untouched for live operation. The HACS
integration is intended to run in parallel first and must be compared against the
legacy command mode/power before any homeserver migration.

## Planned Config Blocks

- Betrieb
- Strategie
- EV
- Batterie-Limits
- Preis & PV-Ökonomie
- Manuelle Steuerung

## Required External Integration

Tibber Prices is the required price source. The HACS module must not call Tibber
Core entities or the Tibber API directly for prices.

## Initial Parallel Pass Criteria

The helper in `custom_components/battery_strategy/parallel.py` marks a run as
passed when:

- at least 12 comparable samples exist
- command mode match ratio is at least 95%
- max command power delta is at most 100 W

These thresholds are intentionally conservative for the first side-by-side run.

## First Parallel Scope

The first comparison should be made with the current live operating assumptions:

- grid charging disabled
- discharge strategy set to `Immer bei Last`
- no price-sensitive discharge
- manual mode off
- EV policy: PV first to car, battery must not feed car

This is intentionally simpler than the full optimizer. The first pass should
compare only live command mode and command power against the legacy package. Once
that holds, price-sensitive charging/discharging and feed-in-tariff opportunity
cost tests can be enabled separately.

## Read-Only Parallel Sensors

The initial integration creates read-only sensors only. The most important ones
for the first real parallel run are:

- Battery Strategy Mode
- Battery Strategy Command Power
- Battery Strategy Reason
- Battery Strategy Residual With EV
- Battery Strategy Residual No EV
- Battery Strategy PV Surplus
- Battery Strategy Allowed Discharge Load
- Battery Strategy Parallel Samples
- Battery Strategy Parallel Mode Match
- Battery Strategy Parallel Max Power Delta
- Battery Strategy Parallel Passed

For the first homeserver test, leave `send_commands` disabled. The legacy package
continues controlling the battery while the HACS integration only observes and
compares.

## Follow-Up Implementation Order

1. Add Home Assistant entity mapping and state collection.
2. Add sensor entities for live diagnostics.
3. Add service handlers for manual override services.
4. Add Tibber Prices discovery/validation in the config flow.
5. Run the new integration in read-only mode next to the legacy package.
6. Evaluate parallel commands with `evaluate_parallel_commands`.

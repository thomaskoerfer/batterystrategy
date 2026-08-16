# Impact analysis: forecast shadow baseline

## Reason and evidence

The target forecast contract required P10/P50/P90 values although the current
production model only computes a point forecast. Filling all quantiles with the
same value would misrepresent model confidence. The load forecast also uses the
current heat-pump power as a near-term feature, but the original contract had no
explicit place for current exogenous load context.

The PV plant contract remains unchanged. The May 2026 capacity change is older
than the active 60-day learning window, so a permanent capacity timeline would
be speculative complexity. Historical backtests that cross a capacity change
remain responsible for explicit normalization in their data preparation.

## Semantic impact

- P50 remains the required slot-energy forecast.
- P10 and P90 are optional until calibrated from matured forecast residuals.
- Calibrated quantiles carry their sample count and must be supplied as a pair.
- Load context is an extensible tuple of normalized semantic drivers. Concrete
  Home Assistant entity IDs remain adapter concerns.
- Forecast and optimizer units, signs, slot alignment and ownership do not
  change.

## Dependency impact

Only the unused target contracts and their tests change initially. Production
models, optimizer output, plan compilation, live control, dashboards, stored
optimizer decisions and actuation are unaffected. The optimizer state gains a
bounded diagnostic trace with one compact parity record per quarter-hour and
14-day retention. The first shadow implementation will populate only the
`heat_pump` driver.

## Decision and safety impact

There is no production decision impact. Shadow outputs are diagnostic only and
cannot reach `BatteryActuator`. Shadow failures must be contained and must not
fail or delay delivery of the existing production plan.

## Compatibility and migration

The contract is not yet persisted or consumed by production code. This is an
intentional pre-cutover breaking correction, so no storage schema bump or data
migration is required. Any future persisted forecast envelope will use the
corrected shape from its first version.

## Verification

- Contract tests cover point-only and calibrated forecasts.
- Shadow and production forecasts receive one captured runtime input set.
- Slot grids must match exactly.
- P50 load and PV differences must remain within 1 W per slot.
- Existing plan, directive, live-command and actuator tests remain unchanged.

## Rollout and rollback

The production forecast remains authoritative throughout the observation
window. Rollback removes the shadow call and restores commit `50e2814`, tagged
as `pre-forecast-shadow-20260816`. No persisted production data requires
migration on rollback.

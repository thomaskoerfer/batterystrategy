# Impact analysis: bounded weather stale-if-error cache

## Reason and scope

Transient weather-provider failures removed the complete normalized weather
snapshot even when a recent successful response covered the same forecast grid.
This unnecessarily degraded optional load-component context. Live control and
the actuator do not consume weather.

## Behavior

The central weather adapter may reuse the last successful response for the same
requested slot grid for at most six hours. Reused slots retain their values and
gain the existing `estimated` quality flag. The original provider error remains
visible in diagnostics and a new fetch is attempted on every quarter-hour
refresh. A different grid or an older cache fails normally and produces missing
weather.

No forecast, optimization, plan, live-control or actuator contract changes.
The patch uses existing weather and quality contracts and introduces no new
provider or installation dependency.

## Verification and rollback

Tests cover successful caching, transient reuse, estimated quality and expiry.
Rollback restores the previous adapter and coordinator files; no persisted
schema or feature-store migration is required.

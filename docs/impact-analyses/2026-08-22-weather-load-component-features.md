# Impact analysis: weather-aware load-component forecasting

## Reason and evidence

The Phase-3 feature store can persist named component energy, but it cannot yet
reproduce forecasts that depend on DHW temperature, outdoor temperature,
circulation state or room temperatures. The existing heat-pump context is a
single current-power value and does not distinguish DHW from space heating. The
house has one shared AC power meter for four LG indoor units, so treating each
room as an independent metered load would multiply measured consumption.

## Approved semantics

The owner approved this extension on 2026-08-22. `LoadForecaster` receives the
same normalized `WeatherSlot` snapshot as the PV forecaster. Component I/O is
completed before forecasting, and component forecasters remain synchronous and
pure. `LoadFeatureValue` carries canonical numeric current and historical
features; feature keys include their units and historical values are
time-weighted slot means with independent coverage.

Heat-pump measurement is split into stable `heat_pump_dhw` and
`heat_pump_space_heating` components by the activity entity. DHW context includes
tank temperature, target, hysteresis, charging, circulation and allowed daily
windows. Space heating has separate activity and target-flow context. AC is one
`air_conditioning` energy component from the shared meter, enriched by aggregate
context from any number of indoor climate entities. A generic metered profile
supports future independently measured loads without changing these models.

The general component is the non-negative residual of whole-house EV-free load
minus all valid configured component energies. Until all configured components
have seven complete days, the shadow retains the existing whole-house forecast
and emits zero learned component contributions. This avoids double counting and
does not alter production decisions during warm-up.

## Storage and compatibility impact

The feature-store envelope advances from schema 2 to 3. Schema 1 and 2 remain
readable. Migration writes an exact `.schema1.bak` or `.schema2.bak` before an
atomic schema-3 replacement. A tested schema-3 to schema-2 downgrade preserves
all slots and component energy while dropping only component features; schema-1
downgrade remains available. No Home Assistant Recorder history is modified.

Load profiles use Home Assistant config subentries, so adding a future component
does not expand the global battery options form. Model coefficients are learned
and intentionally absent from configuration. Open-Meteo is the single weather
provider and is cached centrally; provider failure is a shadow diagnostic only.

## Decision and safety impact

This increment remains Phase-3 shadow-only. Production forecasting,
optimization, plan compilation, live control and the single actuator path are
unchanged. Weather is refreshed in a background task and is never awaited by
the ten-second control calculation. Missing weather or component states cannot
produce a battery command.

## Verification and rollback

Verification covers schema-1/2 migration, schema-2 downgrade, feature
aggregation, missing-component handling, heat-pump activity splitting, one-time
AC meter accounting across four rooms, central weather alignment/cache/failure
quality, exact component summation and no warm-up double counting. The complete
production regression suite must remain green.

Rollback first downgrades the feature store to schema 2 or restores the exact
schema-2 backup, then restores the prior integration release. Config subentries
may remain stored but are ignored by the prior release. Deployment and the
seven-day component observation window require a separate server cutover.

## Status

- Proposed: 2026-08-22
- Approved by owner: 2026-08-22
- Implemented locally: 2026-08-22
- Observed: pending deployment and seven complete days

# Home Assistant runtime hardening impact analysis

## Decision

Harden the Home Assistant application boundary after the Phase-7 architecture
cutover. This change precomputes operator-facing entities, removes large profile
attributes from Recorder persistence, adopts config-entry runtime ownership,
uses the approved actuator port directly and persists active-slot compiler
progress across reloads.

## Contract impact

No approved domain-contract semantics change. `BatteryPlan`,
`PlanLiveDirective`, `LiveMeasurements`, `LivePolicy`, `BatteryCommand` and
`ActuationResult` retain their existing fields and meaning. The concrete
actuator now conforms to the already approved interface rather than accepting a
package-local command type. Compiler persistence is a Home Assistant adapter;
the pure compiler still receives explicit state and progress only.

The fail-closed restart behavior implements the existing requirement that an
in-slot restart must not invent or reopen economic permission. Allowing actual
PV export capture while commercial progress is unrecoverable preserves the
existing distinction between noncommercial PV-follow and paid plan commitment.

## Runtime and operator impact

- Existing entity identifiers and values remain stable.
- Dashboard profile attributes remain available at runtime but are not written
  to Recorder history.
- Integration services remain domain-scoped and survive config-entry reloads
  without duplicate registration.
- A clean reload resumes the same-slot commitment after the actuator is safely
  stopped during unload.
- An unclean restart with unavailable or reset battery counters blocks paid
  charge and discharge for at most the remainder of the current 15-minute slot.

## Persistence and rollback

The new Home Assistant storage document is compact, per config entry, versioned
and atomically written. It contains no credentials, entity identifiers, prices,
forecasts or installation metadata. Downgrading to the previous release simply
leaves the unused storage document in place; it does not alter optimizer state,
feature history or Recorder data.

## Verification

- Full unit, contract and architecture suite.
- Clean reload continuation with same-slot budget consumption.
- Crash recovery from monotonic charge and discharge counters.
- Commercial fail-closed behavior when progress is unrecoverable.
- PV-follow availability during that bounded fail-closed interval.
- Generic actuator contract, safe direction sequencing and one-writer check.
- HACS and Hassfest validation before deployment.

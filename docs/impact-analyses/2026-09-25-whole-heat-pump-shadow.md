# Impact analysis: whole-heat-pump forecast shadow

## Scope

Add a non-authoritative forecast candidate for the complete heat pump. The
existing DHW model is retained inside the candidate while a separate
space-heating model uses causal operating-state and weather evidence. The
authoritative forecast, Scenario Builder, optimizers, compiler, live controller
and actuator remain unchanged.

## Contract assessment

No executable interface contract changes. The existing `ForecastRequest`,
`HistoricalFeatureSlot`, `LoadForecastContext`, weather and load-component
types already carry the required normalized values. `heating_active_age_s` is
an additive feature key under the existing extensible feature contract.
Unknown features remain ignorable.

`ProductionForecastResult`, `PlanningRunOutcome` and the trace scheduler gain
an internal immutable evaluation request. It is not an input to optimization or
control. Forecast trace schema 7 gains an optional observation block; this is a
versioned evaluation format, not a production-layer contract change.

## Runtime isolation

- Production forecasting only captures immutable candidate inputs.
- Candidate calculation starts after authoritative plan persistence and cache
  publication.
- Candidate exceptions are contained and recorded by type.
- The existing optimizer shadow continues to consume only the authoritative
  forecast, preventing attribution ambiguity.
- No Home Assistant entity or Recorder attribute is added.

## Model boundary

The candidate models DHW and space heating as separate electrical-load
components. Space heating uses outdoor temperature, target flow temperature,
time/calendar context, current heating state, active-run age and current
electrical input power. It does not use prices, battery state, PV, EV, optimizer
output or live commands. DHW compressor occupancy suppresses simultaneous space
heating and exposes deferred recovery in later slots.

## Storage and evaluation

One additional compressed candidate block is stored in the existing bounded
quarter-hour trace. Existing 21-day and 64 MiB caps remain authoritative. The
offline forecast evaluator reads schemas 1 through 7 and reports the candidate
components and combined heat-pump series separately.

The first decision point is after seven complete local days with at least three
space-heating runs and three DHW cycles. Promotion is manual and requires a
separate impact analysis. The candidate must improve combined heat-pump MAE and
absolute bias without materially regressing DHW, and its lead-time behavior and
interval coverage must be inspectable.

## Rollback

Reinstall the preceding release. Schema-7 trace files are observational and are
ignored by older runtime code. Removing them is unnecessary for safe rollback.

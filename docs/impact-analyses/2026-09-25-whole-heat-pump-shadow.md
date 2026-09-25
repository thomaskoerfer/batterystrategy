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
control. Forecast trace schema 8 gains an optional observation block; this is a
versioned evaluation format, not a production-layer contract change.

## Runtime isolation

- Production forecasting only captures immutable candidate inputs.
- Candidate calculation starts after authoritative plan persistence and cache
  publication.
- Candidate exceptions are contained and recorded by type.
- No optimizer shadow runs in the cutover. The authoritative optimizer has
  already completed before this forecast-only candidate executes.
- No Home Assistant entity or Recorder attribute is added.

## Model boundary

The candidate models DHW and space heating as separate electrical-load
components. Space heating uses outdoor temperature, target flow temperature,
time/calendar context, current heating state, active-run age and current
electrical input power. It does not use prices, battery state, PV, EV, optimizer
output or live commands. Forecast DHW energy and historical DHW active power
estimate compressor occupancy. Only additional occupancy relative to comparable
historical slots suppresses simultaneous space heating and exposes deferred
recovery in later slots. The initial candidate emits P50 only; forecast
quantiles require later calibration from matured candidate residuals. Active
run continuation remains bounded by compressor time left after forecast DHW;
missing DHW active-power evidence is explicitly estimated.

## Storage and evaluation

One additional compressed candidate block is stored in the existing bounded
quarter-hour trace. Existing 21-day and 64 MiB caps remain authoritative. The
offline forecast evaluator reads schemas 1 through 8 and reports the candidate
components and combined heat-pump series separately.

The evaluator returns `insufficient_data`, `pass` or `fail`. The first decision
point is after seven complete local trace days with at least three space-heating
runs and three DHW cycles. Promotion is manual and requires a separate impact
analysis. Paired slot evidence must improve combined heat-pump MAE, not worsen
absolute bias, keep the local-day block-bootstrap upper confidence bound for
the MAE delta non-positive, and keep DHW MAE within 0.005 kWh per slot of the
authoritative model. Candidate status and missing evidence remain explicit.
Only fully bracketed events inside complete configured local days count toward
the cycle requirements, and at least 95% of configured candidate vintages must
be `ready`.

## Rollback

Reinstall the preceding release. Schema-8 trace files are observational and are
ignored by older runtime code. Removing them is unnecessary for safe rollback.

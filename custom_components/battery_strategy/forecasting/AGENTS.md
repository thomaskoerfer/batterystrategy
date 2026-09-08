# Forecasting package rules

This package owns pure forecasting from normalized inputs.

- Keep load and PV implementations, features, configuration and learned state
  independent.
- Keep named load components isolated; compose them with residual household
  load without double subtraction.
- Accept history, weather, current context and request explicitly.
- Never read Home Assistant, recorder storage, files, network, prices, battery
  SoC or optimizer state.
- Emit aligned immutable forecasts with model version, cutoff and quality.
- Treat missing data as missing, not zero.
- Use semantic feature keys and setup-neutral fixtures.
- Keep historical DHW timing and slot shape as the long-range prior. Recompute
  the next event's energy from complete, physically compatible cycles even when
  its prior timing remains unchanged; do not add an unvalidated recursive
  tank-temperature simulation.
- Treat an activity gap or unusable energy interval as invalidating the whole
  DHW training cycle. Missing boundary temperatures or a missing recorded target
  on a material active sample also invalidate it. Ignore target readings from a
  sub-threshold transition ramp, where interval aggregation can mix inactive
  and active source states. A target change is a new thermostat regime and must
  learn fresh evidence; never reinterpret records from another target by
  matching their observed peak.
- Keep event-timing features separate from cycle-energy features. Time and day
  type may select when a cycle occurs; electrical `kWh/K` may depend on thermal
  lift, source temperature and circulation, but not on clock time by proxy.
- Evaluate timing, event energy and slot placement separately. A timing change
  must improve start error without adding missed cycles. An energy-only change
  must improve cycle-energy error and bias; report any slot-error tradeoff
  caused by unchanged timing rather than hiding it in a horizon-wide total.
- Do not add provider/source identity to the pure model. Equivalent normalized
  sources share learning; a known semantic correction with unchanged feature
  keys requires an operational feature-history reset and explicit reevaluation.
- Treat operator-started finite appliances as event forecasts: inactive P50 is
  zero, and an active cycle may forecast only a bounded remainder learned from
  completed, quality-valid cycles.
- Require cyclic-appliance walk-forward evidence for remaining energy, slot
  shape and end-slot timing before changing its model.

Run deterministic replay, model-isolation, component-composition, uncertainty
and load/PV quality tests for changes in this package.

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
  DHW training cycle. A target change is a new thermostat regime unless the
  observed cycle peak proves that the same cut-out was physically reached.
- Accept a DHW timing change only after walk-forward history improves both
  start-time and slot-energy error without adding missed cycles.

Run deterministic replay, model-isolation, component-composition, uncertainty
and load/PV quality tests for changes in this package.

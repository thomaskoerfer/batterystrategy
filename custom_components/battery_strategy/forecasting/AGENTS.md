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

Run deterministic replay, model-isolation, component-composition, uncertainty
and load/PV quality tests for changes in this package.

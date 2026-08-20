# Impact analysis: remove inline forecast rollback path

## Reason and evidence

The extracted forecaster was parity-proven before cutover and remained the
authoritative source throughout the Phase-1 observation window. No fallback or
unexplained forecast divergence was observed. Keeping duplicate forecast
mathematics would now add drift and maintenance risk without operational value.

## Semantic impact

Load and PV P50 values, weather inputs, learned bias, PV normalization and
tomorrow-energy scaling remain unchanged. The extracted forecaster is the sole
producer of optimizer forecast slots. A forecast exception now propagates to
the existing global fail-safe, which preserves the last valid output and marks
the run as an error; no synthetic or second-path forecast is generated.

## Dependency and decision impact

The optimizer, plan compiler, commercial discharge budgets, live controller,
EV/PV policies, manual switches and actuator are unchanged. Forecast
diagnostics retain source, model version, slot count and runtime. Obsolete
parity/shadow traces are removed on state-schema migration from 7 to 8.

## Verification and rollback

- Production forecast values remain covered by deterministic regression tests.
- Forecast failures are verified to propagate to the global fail-safe boundary.
- Tests prove old comparison traces are removed and cannot regrow.
- The forecasting package remains independent of runtime and actuation code.
- Release `0.2.0-beta.13` remains the immediate package rollback.

Deployment requires one short Home Assistant restart after backing up the
installed integration and optimizer state. No database, dashboard, entity or
hardware-control migration is required.

# Impact analysis: Phase-2 shadow feature store

## Reason and evidence

Forecasting still depends on recorder-derived transitional samples. Phase 2
needs an independent, compact history before any production consumer can be
migrated. Raw ten-second persistence would repeat the recorder growth problem,
so the new path stores one finalized record per quarter-hour.

## Semantic and contract impact

Each UTC-aligned slot stores non-negative kWh for EV-free house load, PV,
grid import/export, battery charge/discharge and EV charging, plus the
time-weighted price in ct/kWh. Battery power is positive for discharge and
negative for charge only at the observation adapter; persisted flows are split.
`QualityFlag.MISSING_PRICE` is an additive contract extension. The initial
persisted envelope uses contract schema 1 because no earlier feature-store
records exist.

## Dependency and decision impact

The coordinator supplies normalized live snapshots to an isolated aggregator.
The compressed file adapter writes finalized slots atomically with 180-day
retention. Forecasting, optimization, plan/budget generation, live control,
manual policies, savings and actuation do not read the store, so no decision or
hardware behavior can change.

## Verification

- Time-weighted flow reconstruction, EV subtraction and battery sign splitting
  have direct unit tests.
- Long gaps are not extrapolated and produce reduced coverage with quality
  flags.
- Persistence tests cover schema validation, upsert deduplication, retention,
  compression and reload.
- Diagnostics expose counts, coverage, flags, size and errors but no raw data.
- The full existing control and optimizer regression suite must remain green.

## Rollout and rollback

Release `0.2.0-beta.15` requires one integration deployment and Home Assistant
restart. Rollback to `0.2.0-beta.14` leaves the standalone feature file unused;
deleting that file is optional and has no effect on existing learned optimizer
state. The Phase-2 gate requires at least seven days of complete slot coverage,
bounded disk growth and unchanged commands, forecasts and savings.

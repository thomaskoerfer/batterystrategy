# Impact analysis: Phase-3 feature-store forecast shadow

## Reason and evidence

Phase 2 has collected complete, energy-balanced 15-minute features independently
of Home Assistant Recorder. The next migration step must prove that those
features can drive the extracted forecast mathematics before Recorder access is
removed. Load and PV also need separate ownership so future changes to PV, base
house load or an individually measured device cannot change another forecast
component implicitly.

## Semantic impact

Production forecast values and ownership do not change. The feature-store path
runs in shadow and remains non-authoritative. It uses only finalized slots ending
at or before `as_of_ms`; no Recorder fallback is allowed. Readiness requires at
least seven days and 672 usable slots for both load and PV, but comparisons are
recorded while the path is warming up.

Load and PV implementations now have separate modules and configuration types.
`LoadForecast` gains optional named components which must share its grid and sum
exactly to total P50. The current model publishes one component,
`general_house_load`. `HistoricalFeatureSlot` gains optional named device-energy
components. Their sum cannot exceed EV-free whole-house load. This permits a
future `heat_pump` or air-conditioning forecaster to be trained and replaced
without importing PV logic or changing the optimizer contract.

## Dependency impact

- Feature aggregation and persistence accept optional load components; current
  adapters supply none, so measured totals are unchanged.
- The persisted feature envelope advances from schema 1 to 2. Schema-1 files are
  read with an empty component tuple and are rewritten as schema 2 on the next
  normal finalized-slot upsert. No backfill or database mutation is required.
- The extracted production facade composes independent load and PV results into
  the unchanged `ForecastBundle` consumed by the optimizer.
- A bounded 14-day gzip trace stores one comparison per UTC quarter-hour outside
  HA Recorder. The next completely future slot is matured against its finalized
  actual to compare production and shadow MAE.
- Diagnostics expose only bounded readiness, parity and accuracy summaries, not
  forecast arrays or feature payloads.

## Decision and safety impact

There is no intended decision impact. Feature-derived outputs cannot reach the
optimizer, plan compiler, live controller or actuator. Shadow component errors
are isolated from each other, and all shadow or trace errors are caught before
the production result is cached. Tests compare complete production plan points
with and without a failing shadow path.

## Compatibility and verification

The in-memory and persisted contracts change additively, with a dual-read
schema migration. Verification covers:

- unchanged production forecast regression values;
- separate load/PV configuration and explicit load-component composition;
- strict training cutoff with future feature slots excluded;
- warming-up/readiness metadata;
- shadow failure isolation and unchanged optimizer plan points;
- trace deduplication, actual maturation, retention and atomic persistence;
- schema-1 feature-store read and schema-2 rewrite;
- the complete optimizer, plan, live-control and actuator regression suite.

## Rollout and rollback

The change is Phase 3 shadow-only. It may be deployed before the Phase-2 cutover
gate because production continues to use Recorder-derived samples. Phase 4
remains blocked until seven to fourteen days of acceptable parity, mature MAE
and no unexplained energy imbalance are observed.

Rollback restores `0.2.0-beta.18`. The schema-2 feature file remains readable by
the new release; rolling back requires restoring the exact pre-deployment
feature-file backup alongside the integration because beta.18 accepts schema 1
only. The standalone shadow trace may be left unused or removed.

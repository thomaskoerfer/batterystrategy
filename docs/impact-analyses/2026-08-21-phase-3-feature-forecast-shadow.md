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
components with independent quality metadata. Meter reconciliation differences
are flagged rather than rejected. This permits a
future `heat_pump` or air-conditioning forecaster to be trained and replaced
without importing PV logic or changing the optimizer contract.

## Dependency impact

- Feature aggregation and persistence accept optional load components; current
  adapters supply none, so measured totals are unchanged.
- The persisted feature envelope advances from schema 1 to 2. The complete store
  is atomically migrated, with a pre-migration backup and a tested schema-2 to
  schema-1 down-migration that preserves all slots while dropping only component
  metadata.
- The extracted production facade composes independent load and PV results into
  the unchanged `ForecastBundle` consumed by the optimizer.
- A bounded 14-day gzip trace stores compact typed comparison points for fixed
  lead-time classes up to about 24 hours outside HA Recorder. Points mature
  against finalized actuals to compare production and shadow MAE by lead time.
- Diagnostics expose only bounded readiness, parity and accuracy summaries, not
  forecast arrays or feature payloads.

## Decision and safety impact

There is no intended decision impact. Feature history feeds a dedicated shadow
runner directly; neither history nor shadow results reach the optimizer, plan
compiler, live controller or actuator. Shadow component errors
are isolated from each other, and all shadow or trace errors are caught before
they leave the runner. Tests enforce that optimization has no shadow-history or
evaluation dependency and that runner failures remain diagnostics only.

## Compatibility and verification

The in-memory and persisted contracts change additively, with a dual-read
schema migration. Verification covers:

- unchanged production forecast regression values;
- separate load/PV configuration and explicit load-component composition;
- strict training cutoff with future feature slots excluded;
- warming-up/readiness metadata;
- shadow failure isolation and unchanged optimizer plan points;
- trace deduplication, actual maturation, retention and atomic persistence;
- schema-1 to schema-2 migration, pre-migration backup and schema-1 downgrade;
- the complete optimizer, plan, live-control and actuator regression suite.

## Rollout and rollback

The change is Phase 3 shadow-only. It may be deployed before the Phase-2 cutover
gate because production continues to use Recorder-derived samples. Phase 4
remains blocked until at least seven complete days of separate load/PV review,
mature lead-time metrics and no unexplained energy imbalance are observed. The
cutover requires a separate owner approval.

Rollback restores `0.2.0-beta.18`. The schema-2 feature file remains readable by
the new release; rolling back first runs the schema-1 down-migration or restores
the exact pre-migration backup because beta.18 accepts schema 1 only. The
standalone shadow trace may be left unused or removed.

## Owner approvals

Approved by the owner on 2026-08-21: load-component semantics and schema 2;
temporary heat-pump context behavior; direct Feature Store to shadow-runner
connection; atomic up/down migration; the explicit contract approval lifecycle;
the minimum seven-day Phase-3 evaluation gate; component model/training/quality
metadata; removal of operational PV-capacity timelines; compact lead-time
evaluation; and the typed forecast-evaluation boundary.

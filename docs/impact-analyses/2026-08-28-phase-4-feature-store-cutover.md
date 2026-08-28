# Impact analysis: Phase-4 feature-store forecast cutover

## Reason and evidence

Phase 3 proved the recorder-independent feature pipeline and separate load/PV
models in parallel. The owner explicitly approved a direct local cutover rather
than an additional production selector: the feature store becomes the only
forecast source, while Git remains the rollback mechanism.

## Semantic and contract impact

The approved contracts do not change. `HistoricalFeatureSlot`,
`ForecastRequest`, `LoadForecastContext` and `ForecastBundle` retain their units,
signs, slot alignment, quality semantics and ownership. The change is in runtime
composition: forecasting produces one immutable `ForecastBundle` before the
economic plan is built, and optimization consumes that bundle without selecting
or invoking a forecaster.

Feature history is restricted to finalized slots ending at or before
`ForecastRequest.as_of_ms`. Production readiness requires at least 672 usable
load slots, 672 usable PV slots and seven days of history. When separate load
components are configured, at least 672 slots must contain all configured
components. Failure is explicit; there is no Recorder forecast fallback.

## Dependency and decision impact

- The coordinator owns the current feature-store snapshot, weather and device
  context and passes immutable values through the optimizer adapter.
- Load and PV are built independently and composed into the existing bundle.
- Existing price optimization, budget generation, plan compilation, live
  control, safety logic and actuation are unchanged.
- One-hour forecast diagnostics now derive from the same production bundle as
  the plan, eliminating mixed old/new dashboard values.
- The former live shadow runner is removed from runtime composition. Its pure
  evaluation code remains available for offline regression analysis.

Forecast values can change because their historical source changes and separate
load components become authoritative. Any resulting plan change is therefore a
forecast consequence, not a new optimizer or live-control rule.

## Compatibility and verification

No persisted schema or config-entry migration is required. Existing schema-3
feature files remain authoritative. Verification covers contract construction,
strict training cutoff, readiness failure, slot-grid equality, explicit bundle
injection, independent load/PV tests, optimizer regression tests and the full
integration suite. A retained-history replay and component-readiness review are
mandatory before deployment.

## Rollout and rollback

Implementation is local only on `codex/phase-4-feature-cutover`. It must not be
installed on Home Assistant until the owner explicitly confirms deployment.
There is no old/new UI option and no runtime dual path. Rollback before or after
deployment restores release `0.2.0-beta.21` / commit `42e4f57`; the feature store
is unchanged and may continue collecting on that version.

## Approval

Direct local Phase-4 implementation, no permanent selector/fallback and delayed
deployment were approved by the owner on 2026-08-28. This analysis changes no
interface-contract semantics, so no additional contract approval is required.

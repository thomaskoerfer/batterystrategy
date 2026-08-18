# Changelog

All notable changes to Battery Strategy are documented here.

## Unreleased

## [0.2.0-beta.12] - 2026-08-18

### Fixed

- PV-recovery discharge budgets now open only when confidence-weighted future
  PV surplus exceeds physical battery headroom plus the uncertainty reserve.
- Forecast export caused by optimizer discretization or an economic plan choice
  no longer creates discharge permission by itself.

### Changed

- Isolated the PV spill calculation in a pure, directly tested optimizer
  function and removed the superseded planned-export recovery path completely.

### Safety

- Later expensive-load reservation, slot budget caps, live PV priority, EV
  policy and battery actuation are unchanged.

## [0.2.0-beta.11] - 2026-08-18

### Fixed

- Future charging now reduces higher-value load reserves only by the energy it
  can actually replace. A small forecast PV charge no longer releases all
  battery inventory reserved for later expensive household load.
- Forecast PV replacement receives the existing PV-confidence discount; firm
  grid recharge remains fully credited.

### Safety

- The slot budget cap, plan compiler, live meter following, PV/EV policy and
  actuator are unchanged. This release only corrects the optimizer's
  commercial discharge permission.

## [0.2.0-beta.10] - 2026-08-17

### Fixed

- Future SoC, power and discharge-budget profiles now remain canonical
  optimizer output. Live command deviations are no longer projected into the
  next plan slot or applied as a constant shift to the remaining horizon.
- Removed the display-only live overlay that could show planned discharge after
  the displayed SoC had already reached its minimum.

### Safety

- The economic optimizer, plan compiler, live controller, PV/EV policy and
  actuator are unchanged. Actual live deviations feed the next regular planning
  run through measured battery SoC.

## [0.2.0-beta.9] - 2026-08-17

### Fixed

- Removed artificial monetary costs for optimizer plan-mode transitions. Scarce
  battery energy now stays assigned to the objectively most valuable slots
  instead of being moved to cheaper slots to avoid a fictional stop/start.
- Guaranteed that every planned discharge is covered by the slot's explicit
  commercial discharge budget.
- Cleared timezone and PV-capacity caches when runtime configuration changes.

### Safety

- RTE, minimum margin and micro-cycle suppression remain the economic cycle
  guards. PV follow, EV policy, live meter following and battery actuation are
  unchanged.
- A discharge budget may still coexist with free PV charging when later PV
  export creates real headroom value; live PV surplus continues to take
  precedence over planned discharge.

## [0.2.0-beta.8] - 2026-08-16

### Added

- Documented and executable interface contracts for data, forecasting,
  optimization, plan compilation, live control and actuation boundaries.
- Contract tests for units, slot alignment, forecast grids, market signs and
  fail-closed command semantics.
- Contract governance requiring impact analysis while explicitly allowing the
  target-architecture contracts to evolve as migration evidence improves them.
- Forecast contracts now support an immediately available P50 point forecast,
  progressively calibrated P10/P90 bounds and extensible current load-driver
  context without device-specific Home Assistant coupling.
- Added an isolated load/PV shadow forecaster that compares against the
  unchanged production forecast using one captured input set.
- Added bounded 14-day quarter-hour parity diagnostics outside the Home
  Assistant recorder, with no shadow path to optimization or actuation.

### Safety

- Shadow failures are contained and cannot prevent the production optimizer
  from returning its existing plan.
- Production forecast values remain authoritative throughout the observation
  window.

## [0.2.0-beta.7] - 2026-08-15

### Changed

- Recorder history now uses Home Assistant's configured recorder engine only;
  there is no SQLite fallback on MariaDB or other recorder backends.
- Optional entity mappings can be cleared in the reconfigure flow.
- Removed unused pre-HACS forecast, persistence and Tibber storage helpers.
- Documented that Tibber Prices is price-only and never a grid-power fallback.

### Fixed

- PV headroom discharge budgets remain consistent with planned charging.

## [0.2.0-beta.6] - 2026-08-04

### Changed

- Entity mappings now use Home Assistant's reconfigure flow and perform one
  managed integration reload.
- Strategy options use `OptionsFlowWithReload`.

### Safety

- Active battery limits are synchronously set to zero before an integration
  reload or unload. Disabled control remains hands-off.
- Config-entry version 2 persists the restored EV/PV priority policy during
  upgrades.

### Tests

- Added upgrade, flow-lifecycle and safe-unload regression coverage.

## [0.2.0-beta.3] - 2026-08-03

### Fixed

- Actual-savings accounting now resolves the integration's recorded battery-power
  entity through Home Assistant's entity registry. This restores the correct
  split between PV and grid charging after the HACS runtime refactor, including
  installations where users renamed the generated sensor.

## [0.2.0-beta.2] - 2026-08-03

### Added

- Non-blocking background planning while the live meter-following loop continues every 10 seconds.
- One-shot fail-safe zeroing for stale grid inputs or an unavailable battery SoC.
- Atomic compressed optimizer-state persistence with migration from legacy plain JSON.
- Separate optimizer policies and limits for PV charging, grid charging, and discharging.
- Regression coverage for background planning, measured slot budgets, persistence migration, and fail-safe behavior.

### Changed

- Slot charge and discharge progress now uses measured battery power instead of commanded power.
- The configured planning horizon is applied by the production optimizer and limited to the available 48-hour price horizon.
- Obsolete YAML setup and unused EV-priority/manual-duration controls were removed.
- The test-only duplicate optimizer was removed; tests target the production engine.
- HACS repository metadata and release automation were updated.

### Safety

- A fresh installation still starts with battery control disabled.
- Disabling control zeros both limits once and then remains hands-off for manual battery operation.
- Persisted SoC bridges only a short startup gap; prolonged SoC loss stops active control.

## [0.2.0-beta.1]

- Initial HACS beta packaging of the existing Battery Strategy implementation.

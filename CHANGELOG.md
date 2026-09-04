# Changelog

All notable changes to Battery Strategy are documented here.

## [0.2.0-rc.10] - 2026-09-04

### Fixed

- Preserve an active-slot compiler commitment across temporary missing or
  incomplete planner results. The affected refresh still fails closed, while a
  subsequent same-slot plan can no longer reopen a prorated discharge budget.
- Round the operator-facing remaining-discharge-budget sensor to Wh precision
  without reducing the precision used by compilation or live control.

## [0.2.0-rc.9] - 2026-09-04

### Fixed

- Preserve proportional discharge permission when Home Assistant starts in the
  middle of a slot without a usable compiler-runtime snapshot. The fallback
  uses only the unelapsed slot fraction and cannot reopen during later replans.
- Keep paid grid charging closed in that fallback. Exact clean snapshots and
  counter-reconstructed unclean snapshots continue to take precedence.

## [0.2.0-rc.8] - 2026-09-04

### Fixed

- Treat change-driven battery power entities as usable while Home Assistant
  reports them available. This prevents unchanged MQTT values from falsely
  expiring after 30 seconds and locking live control at zero after a restart.
- Keep strict age checks for grid, SoC and policy-relevant EV inputs; only the
  battery transport's availability semantics are corrected.

## [0.2.0-rc.7] - 2026-09-04

### Changed

- Isolate active-slot compiler state, measured throughput and restart recovery
  in a dedicated runtime module while keeping scheduling and actuation in the
  Home Assistant coordinator.
- Centralize option defaults, numeric constraints and profile validation so
  config flows and runtime entities no longer maintain parallel policy tables.
- Precompute Home Assistant sensor values and profile attributes once per
  coordinator refresh; entity properties are now read-only projections.
- Exclude all changing profile and plan-table attributes from Recorder while
  keeping them available to dashboards.
- Move per-entry runtime ownership to typed `ConfigEntry.runtime_data` and
  register integration services once during domain setup.
- Align the Zendure adapter with the approved generic
  `apply(BatteryCommand) -> ActuationResult` contract for normal, disabled and
  fail-safe commands.
- Persist compact active-slot compiler state and throughput so config-entry
  reloads cannot reopen consumed charge or discharge permission.
- Carry the optimizer's canonical `BatteryPlan` through an immutable planning
  result instead of reconstructing compiler intent from dashboard profiles.
- Migrate planning state to schema 11 so canonical intent, its authorizing
  execution policy and operator-facing data remain separate across restarts.
- Normalize Home Assistant values once into `LiveMeasurements`, emit the
  compiler contract directive directly and pass the live controller's validated
  `BatteryCommand` unchanged to the single actuator.
- Separate live diagnostics from executable commands and make direction
  hysteresis state explicit in `LiveControlState`.
- Select the current plan slot atomically, preserve measured throughput across
  slot boundaries and publish completed background plans immediately.

### Safety

- Fail closed on unavailable battery and stale SoC, grid and policy-relevant EV inputs,
  retry unconfirmed safety stops, and reject unload while an active battery
  cannot be stopped.

- Unclean same-slot restarts reconstruct throughput from monotonic battery
  energy counters. If that is impossible, commercial charge and discharge fail
  closed until the next slot while PV-follow remains available.
- Optimizer, forecast and commercial compiler semantics are unchanged. This
  release remains local until validation and explicit deployment approval.
- Legacy or malformed schema-10 plan data remains visible to operators but
  fails commercially closed until a fresh optimizer run succeeds.
- Live-model convergence was approved on 2026-09-04 and is implemented as a
  coordinated in-memory contract migration without entity or storage changes.

## [0.2.0-rc.6] - 2026-09-03

### Changed

- Made the deterministic Phase-6 plan compiler the sole plan-directive
  authority after its separately gated shadow and cutover stages.
- Prepared the final architecture-cleanup phase by removing completed forecast
  and optimizer shadow implementations and the superseded economic kernel.
- Replaced direct Recorder-table access with a bounded adapter using Home
  Assistant's public history API; feature-store records now provide long-term
  calibration bootstrap independently of the Recorder backend.
- Added regression guards that prevent a second optimizer path, dormant shadow
  runtime or direct SQL dependency from returning.
- Removed the completed compiler shadow, the previous plan translator and the
  duplicate coordinator slot latch after stacking Phase 7 on the prepared
  compiler cutover.
- Moved every battery-related Home Assistant service call into the dedicated
  actuator boundary without changing write order, retry or safe-zero behavior.
- Split the commercial runtime into cohesive market-context, planning-service
  and measured-savings components without changing approved contracts.
- Kept missing-price savings events pending instead of advancing their energy
  counter baseline without a usable tariff.
- Removed completed forecast, optimizer and compiler comparison files after
  preserving the authoritative learned optimizer state.
- Removed the final compatibility engine and CLI/stdout protocol. Home
  Assistant now invokes the planning pipeline directly through its adapter.
- Replaced mutable module-global planning inputs with an immutable per-refresh
  runtime snapshot and split measurement, tariff, state, forecast application,
  evaluation and presentation responsibilities out of the coordinator pipeline.
- Migrated persisted optimizer state to schema 9 so historical EV samples are
  normalized once rather than through a permanent runtime compatibility branch.
- Removed HA-less and pre-minimum-version import fallbacks; runtime imports now
  match the declared Home Assistant 2026.7 minimum instead of masking packaging
  errors.
- Moved bounded command-trace serialization out of the live coordinator while
  retaining its executor boundary, schema and retention limits.

### Safety

- Phase-6 slot-boundary parity and command-trace gates passed before this
  cleanup removed the non-authoritative comparison paths.
- Forecast, optimizer, plan and live-control contracts are unchanged. Battery
  service calls retain their established order and safe-zero behavior behind
  the single actuator boundary.

## [0.2.0-rc.5] - 2026-09-03

### Fixed

- Prevented grid charging from being deferred past the expensive demand it is
  intended to serve merely because cheaper charging capacity exists later in
  the planning horizon.
- Removed post-optimization micro-cycle filtering that could delete discharge
  while retaining its preceding paid charge; RTE and minimum margin remain
  enforced directly by the economic objective.

## [0.2.0-rc.4] - 2026-09-01

### Fixed

- Kept the last successful normalized weather snapshot for up to six hours
  during transient provider failures instead of immediately removing weather
  context from load-component forecasts.
- Marked stale-if-error weather slots as estimated and retained the provider
  error in diagnostics while continuing quarter-hour refresh attempts.

### Safety

- The fallback is bounded and never affects the live meter-following or
  actuator path. After six hours the adapter returns to explicit missing
  weather rather than silently retaining an old forecast.

## [0.2.0-rc.3] - 2026-09-01

### Changed

- Made the contract-based pure economic optimizer authoritative after 242
  retained shadow runs passed operational parity.
- Kept exact numerical deltas visible while classifying sub-resolution legacy
  serialization differences separately from operational mismatches.
- Adapted the typed `BatteryPlan` to the existing downstream profile format so
  the plan compiler, live controller, dashboards and actuator remain unchanged.

### Safety

- The previous kernel remains non-authoritative for one short rollback window
  and continues to provide comparison diagnostics only.
- Pure-optimizer failure fails the planning run closed; it does not silently
  restore the previous decision path.
- The live controller and actuator are unchanged.

## [0.2.0-rc.2] - 2026-08-30

### Added

- Added the contract-based pure economic optimizer as a non-authoritative
  Phase-5 shadow running at most once per 15-minute slot.
- Added bounded 14-day parity diagnostics for charge, discharge, SoC, budget
  and cost differences outside Home Assistant Recorder.

### Safety

- The existing optimizer remains the sole plan authority; shadow output cannot
  reach the plan compiler, live controller or actuator.
- Shadow calculation and persistence failures are contained and cannot fail an
  authoritative optimizer run.

## [0.2.0-rc.1] - 2026-08-30

### Changed

- Made the finalized feature store the sole production source for EV-free load
  and PV forecasts after an explicit seven-day readiness gate.
- Passed one immutable `ForecastBundle` into optimization and removed the
  production forecast fallback and runtime shadow composition.
- Split fast Zendure-style meter following into a dedicated live controller
  while preserving plan budgets, EV policy and safety limits.

### Safety

- Production forecasting fails closed when feature or component history is not
  ready; it never silently falls back to Recorder-derived forecasts.
- Optimizer economics, plan compilation and actuator safety constraints remain
  unchanged by the forecast cutover.
- The live controller retains stale-input, SoC, slot-budget and strategy-enabled
  gates and has a dedicated hardware-behavior regression suite.

## [0.2.0-beta.21] - 2026-08-22

### Fixed

- Published optimizer-native PV charge, grid charge and required total charge
  instead of reconstructing charge sources from rounded forecast powers.
- Prevented sub-quantum grid remnants in mixed PV slots from becoming paid
  commitments or live `must_charge` commands.
- Kept earlier grid commitments when later cheaper slots lack enough physical
  charge capacity, while preserving PV-first execution under forecast error.

## [0.2.0-beta.20] - 2026-08-22

### Added

- Added configurable, weather-aware shadow load components for DHW, space
  heating, shared-meter air conditioning and generic metered loads.
- Advanced the feature store to schema 3 with component features, migrations,
  backups and a tested schema-2 downgrade.

### Fixed

- Removed the rank-based fictional credit that could make cheap grid charging
  appear less expensive than its real tariff.
- Included round-trip loss and minimum margin before future grid charging may
  release current battery inventory for earlier household load.
- Kept historical battery acquisition cost as savings accounting instead of
  allowing it to lower the forward-looking optimizer discharge floor.
- Included configured export revenue in baseline and optimized plan costs.
- Moved equal-cost grid-charge deferral into a cost-neutral optimizer tie-break
  so the future-slot table and planned SoC show the schedule that will execute.
- Made the current slot's published grid component required live charge instead
  of silently moving it into unpublished future capacity.
- Kept the last measured SoC as a visibly stale planning estimate during longer
  sensor gaps instead of replacing it with an invented 50%, while continuing
  to block actuation and optimizer refreshes until the sensor recovers.

### Safety

- The economic correction is confined to optimization and can only reduce
  low-value discharge plans and budgets. Plan compilation, live meter following,
  EV/PV policy, slot budget consumption and actuation are unchanged.
- Weather-aware component forecasting remains Phase-3 shadow-only and cannot
  reach optimization or battery commands.

## [0.2.0-beta.19] - 2026-08-21

### Added

- Added Phase-3 recorder-independent forecast shadowing directly from finalized
  feature-store slots, with readiness and separate load/PV diagnostics for
  compact lead-time classes up to 24 hours.
- Added a bounded atomic 14-day comparison trace outside Home Assistant
  Recorder.
- Added optional named historical and forecast load components, initially
  publishing `general_house_load`, so separately measured devices can be added
  without coupling them to PV or unrelated load logic.
- Added typed, non-authoritative forecast-evaluation contracts.

### Changed

- Split the extracted house-load and PV forecast implementations and their
  configuration into independently owned modules while retaining numerically
  identical production output.
- Advanced the feature-store envelope to schema 2 with atomic full migration,
  component quality metadata, a pre-migration backup and schema-1 downgrade.
- Removed historical PV-capacity timelines from the operational forecaster; the
  current PV and inverter limits remain explicit physical inputs.

### Safety

- Production forecasting remains authoritative and continues to use its
  existing Recorder-derived history. Shadow outputs cannot reach optimization,
  plan compilation, live control or actuation.
- Load, PV and trace failures are isolated in a dedicated shadow runner. Feature
  history and evaluation results never enter optimization or live control.

## [0.2.0-beta.18] - 2026-08-21

### Changed

- Moved planned grid energy to the front of the future-slot table and changed
  it to EV-free net load before battery action.
- Added the commercial discharge budget before planned discharge so permission
  and expected execution can be compared directly.

## [0.2.0-beta.17] - 2026-08-21

### Changed

- Changed all planned battery-flow columns in the future-slot table from watts
  to slot energy in kWh.
- Added planned net grid energy without EV, using positive values for import
  and negative values for export.

## [0.2.0-beta.16] - 2026-08-20

### Added

- Added a compact future-slot table with price, planned charge and discharge,
  PV/grid charge split and planned SoC for the full planning horizon.
- Added the table as a full-width dashboard view between plan charts and cost
  reporting.

### Data retention

- Marked the table's presentation-only rows and column metadata as unrecorded
  attributes so revised plans do not increase Recorder storage.

## [0.2.0-beta.15] - 2026-08-20

### Added

- Started Phase 2 with a recorder-independent, atomic gzip feature store that
  aggregates irregular live measurements into canonical UTC 15-minute energy
  slots and retains at most 180 days.
- Added quality metadata for coverage, restart gaps and missing grid, PV,
  battery, EV or price inputs, plus bounded store-health diagnostics.

### Safety

- The feature store is explicitly non-authoritative and is never read by the
  forecaster, optimizer, plan compiler, live controller or actuator.
- Aggregation and persistence errors are isolated and reported without
  interrupting battery control. Disk writes occur only for finalized slots.

## [0.2.0-beta.14] - 2026-08-20

### Changed

- Completed Phase 1 by removing the duplicate inline forecast calculation,
  parity gate and comparison trace after the extracted forecast observation
  window succeeded.
- The extracted load/PV forecaster is now the sole optimizer forecast input.
- Simplified forecast diagnostics to source, model version, slot count and
  runtime; optimizer state schema 8 discards obsolete comparison traces.

### Safety

- Forecast mathematics and inputs are unchanged. Optimization, plan and budget
  generation, live control, switches and battery actuation are untouched.
- Forecast failures continue through the existing global fail-safe, which
  publishes the last valid output as `error` instead of inventing a forecast.

## [0.2.0-beta.13] - 2026-08-19

### Changed

- Promoted the parity-proven extracted load and PV forecasters to the
  authoritative optimizer input without changing forecast mathematics.
- Retained the previous inline calculation as a temporary Phase-1 rollback
  path when the extracted forecast errors or differs by more than 1 W.
- Renamed shadow diagnostics to forecast source/parity diagnostics and migrated
  the existing bounded comparison trace without losing observations.

### Safety

- Forecast source selection occurs before optimization. Optimization, plan
  compilation, live control, EV policy and battery actuation are unchanged.
- The inline fallback is explicit, tested and scheduled for removal after the
  extracted-forecast observation window succeeds.

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

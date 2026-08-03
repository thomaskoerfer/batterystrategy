# Changelog

All notable changes to Battery Strategy are documented here.

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

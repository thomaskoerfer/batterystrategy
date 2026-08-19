# Impact analysis: extracted forecast cutover

## Reason and evidence

The extracted load and PV forecasters completed 236 consecutive quarter-hour
comparisons from 2026-08-16 22:30 Europe/Berlin through 2026-08-19 09:15 with
no gaps or errors. Load differences were zero and the maximum PV difference was
below 0.0001 W, satisfying the Phase-0.5 parity criterion.

## Semantic impact

The extracted `ForecastBundle` P50 values become the optimizer's authoritative
load and PV inputs. Units, slot alignment, model mathematics and forecast
contracts do not change. P10/P90 remain absent until actual residuals support
calibration.

The old inline calculation remains temporarily as a rollback path. A build
error or parity deviation above 1 W selects `inline_fallback`; otherwise the
source is `extracted`. This is deliberate rollout protection, not a permanent
second forecasting architecture.

## Dependency and decision impact

Only forecast-source selection changes. Optimizer prices and constraints,
`BatteryPlan`, plan compilation, live control, EV policy and actuation are
unchanged. Numerically identical forecast inputs preserve plans; sub-watt
floating-point differences are explicitly covered by the accepted parity
tolerance and rounded public profiles.

Diagnostics change from `forecast_shadow_*` to `forecast_source` and
`forecast_parity_*`. The persisted `forecast_shadow_trace` is migrated to
`forecast_parity_trace` on first load, preserving all observations. The state
schema advances from 6 to 7; no recorder or dashboard migration is required.

## Verification

- The extracted forecast is proven to supply optimizer points within tolerance.
- Build errors and deviations above 1 W use the inline fallback.
- State migration preserves the previous trace and removes its old key.
- Forecasting retains no Home Assistant runtime or actuator dependency.
- The complete unit, contract, plan, live and actuator suite remains green.

## Rollout and rollback

Release `0.2.0-beta.13` is deployed by replacing the integration package and
restarting Home Assistant once. Release `0.2.0-beta.12` is the immediate
rollback. During observation, diagnostics must report `forecast_source` as
`extracted`, parity as `pass`, and no unexplained plan or command change.

After a successful observation window, remove the inline forecast, parity gate
and one-time legacy trace migration together. Retaining both implementations
indefinitely is explicitly out of scope.

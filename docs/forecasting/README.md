# Forecasting

## Purpose

Forecasting predicts future EV-free household load and PV generation on one
shared quarter-hour grid. It is deterministic and side-effect free once its
history, weather, current context and request have been assembled.

## Independent models

`LoadForecaster` and `PvForecaster` are independent boundaries:

- load forecasting may use historical household load, named load components,
  time/calendar features, weather and current device context;
- PV forecasting may use historical PV generation, plant limits, time/solar
  shape, weather and learned PV bias;
- neither model may read the other's configuration, features or learned state;
- net load is derived from load minus PV and is not a third learned forecast.

Changing PV behavior must not change load output. Adding or changing a heat
pump, air conditioner or generic load component must not alter PV behavior or
unrelated load components.

## Contract

Both forecasters receive a `ForecastRequest` and return aligned immutable slot
tuples. P50 is mandatory. P10/P90 are emitted only as a calibrated pair with
sufficient matured residuals. Every forecast identifies its model version,
training cutoff and quality.

The combined `ForecastBundle` is constructed before optimization. Forecasting
does not know prices, battery SoC, battery constraints, terminal value or a
battery plan.

Production uses concrete configured implementations of both forecast
contracts. The application supplies immutable run-local model configuration,
then a policy-free composer invokes the load and PV owners independently and
only combines their results into `ForecastBundle`. Current calibration inputs
remain application configuration because the approved contracts do not expose
them; moving them into contract data requires a separate impact analysis and
owner approval.

## Load components

The whole-house target always excludes EV charging. Independently metered loads
may be forecast separately and composed with a residual general-house component.
Each component owns its features, warm-up gate, model version and quality.
Missing component data remains in total household load and is never silently
treated as zero or subtracted twice.

Current profiles support heat-pump domestic-hot-water and space-heating context,
a shared-meter multi-zone air-conditioning context and a generic metered load.
Additional profiles must use stable semantic feature keys rather than entity
names or vendor payload fields.

### Heat-pump hot water and space heating

Domestic-hot-water recovery is a finite thermostat cycle, not a persistence
forecast of current compressor power. The configured target is the cut-out
temperature; the cut-in temperature is target minus hysteresis. While a cycle
is active, the model estimates remaining electrical energy from the measured
temperature deficit and robust `kWh/K` evidence from comparable completed
cycles. Comparable cycles are weighted by initial temperature lift, source
temperature, circulation, time of day and day type. Replanning may update that
estimate from new measurements, but it must not restart a fixed-duration
continuation. Cycle efficiency is learned from the electrical energy and the
observed start-to-peak temperature lift. A cycle is training evidence only
after a clean active-to-inactive transition; an activity gap or unusable energy
interval invalidates the complete cycle rather than creating partial cycles.
Recorded targets provide context, while the observed peak proves whether a
cycle physically reached the current cut-out. This separates real thermostat
regimes without retaining source-mapping compatibility branches.
If a live cycle first appears inside an unfinished slot, the model immediately
publishes its complete thermally estimated remaining energy. Energy estimated
since the configured charging or hot-water activity state actually became
active is deducted so repeated forecasts cannot move the cycle end forward. If
that transition time
is unavailable, the model conservatively deducts nothing until finalized
evidence exists. A transient compressor ramp uses learned cycle power for
duration while measured power still contributes the current-slot evidence.

While the compressor is inactive, the historical cycle profile remains the
long-range timing and slot-shape prior. The next cycle may be refined from
comparable historical inactive states using tank temperature, circulation
state, local time and day type. The three nearest independent cycles vote on
the next start. Its total electrical energy is always recalculated from the
expected cut-in temperature, learned peak and weighted cycle efficiency; the
prior shape is then scaled to that total within learned cycle power. Thus a
one-slot timing difference does not move the event, but it also no longer
freezes a stale historical energy total. The correction may move only the
prior's next cycle, must start inside a configured allowed window and cannot
overlap a following prior cycle. These constraints prevent sparse draw events
or sensor noise from causing speculative recursive temperature rollout and
quarter-hour forecast churn. A tank already at or below cut-in is a direct
thermostat trigger and moves the cycle only to the next configured allowed
window. Missing or discontinuous timing evidence leaves the timing prior
intact; complete thermal cycles continue to calibrate its energy.

Outdoor temperature is a performance feature for hot-water recovery because it
can affect COP, electrical energy per kelvin and duration. It is not treated as
the direct cause of a hot-water draw. Humidity is deliberately excluded until a
walk-forward evaluation demonstrates material independent value.

Hot-water and space-heating outputs remain separately observable, but their
shared compressor constraint is modeled together. Predicted hot-water activity
displaces simultaneous space heating. Only the incremental displacement beyond
the historical hot-water profile is deferred into later heating slots, which
preserves the historically learned post-cycle buffer recovery without counting
it twice. Other load components and the PV forecast remain independent.

## PV model

PV output is constrained by configured plant and inverter capability and may use
weather and learned slot bias. Historical plant changes belong to explicit
backtest preparation; operational forecasting must not accumulate permanent
one-household correction branches.

## Setup independence

Models consume normalized contracts, never entity IDs, addresses, device serial
numbers, hostnames or installation URLs. Supported provider/device classes may
be documented, but model semantics must remain portable to another installation
with equivalent normalized inputs.

## Verification

Load and PV are evaluated separately by lead time, time of day, MAE, bias, daily
energy and quality coverage. Tests prohibit prices, SoC and optimizer imports;
check component summation and missing-data behavior; and prove that load and PV
changes cannot affect each other unintentionally.

Hot-water changes additionally require walk-forward comparison against the
released model over every usable retained cycle in the evaluated thermostat
regime. Start-time, slot-energy and total-cycle energy error and bias are
reported. Timing and slot error must improve without increasing missed cycles;
otherwise the candidate is rejected even if its physical model appears
plausible.

Run the same repository script with each code checkout on one copied feature
store and compare the JSON summaries:

```bash
PYTHONPATH="$CHECKOUT" python scripts/battery_strategy_dhw_walkforward.py \
  /path/to/battery_strategy_features.json.gz \
  --timezone Europe/Berlin --target-c 53 --hysteresis-k 9 \
  --allowed-windows '03:00-05:00,09:00-17:00'
```

## Production status

Feature-store forecasting is authoritative. The slot-profile helpers are its
single mathematical source, not a second runtime selector. No alternate
forecast composition or rollback-only runtime path is installed.

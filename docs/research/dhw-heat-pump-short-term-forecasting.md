# Short-Term Forecasting of Domestic-Hot-Water Heat-Pump Charging

## Scope

This note covers domestic-hot-water (DHW) charging by a heat pump. It does not
cover space-heating demand. The distinction matters: space-heating load is
primarily driven by building thermal dynamics and weather, whereas DHW load is
driven by discrete draw-offs, tank state, thermostat control and household
timing.

## Findings

### 1. Forecast thermal demand and tank state, not current electrical power

A storage water heater alternates between draw-off, recovery and standby. A
draw removes hot water from the top and introduces cold water at the bottom;
charging and mixing then change the tank's temperature distribution. A single
bulk temperature therefore does not fully describe the available hot-water
inventory. Both the official EnergyPlus model and validated research models
represent stratification with multiple nodes or reduced two-node models
([EnergyPlus Engineering Reference](https://energyplus.net/assets/nrel_custom/pdfs/pdfs_v24.2.0/EngineeringReference.pdf),
[Shen et al., 2018](https://doi.org/10.1016/j.ijrefrig.2017.10.023),
[Premer et al., 2026](https://doi.org/10.1016/j.enconman.2025.120723)).

For short-horizon control, a low-order model is usually sufficient. A
retrofittable single-sensor model that accounts for stratification during
charging approached the accuracy of a much more expensive multi-layer model
for mean tank temperature in one experimental comparison
([Kepplinger et al., 2019](https://doi.org/10.1016/j.tsep.2018.11.003)). The
practical implication is not that tank physics can be ignored, but that this
repository does not need a detailed CFD or many-node model initially.

### 2. Setpoint and hysteresis define cycle boundaries

For cycling water heaters, the setpoint is the cut-out temperature and
`setpoint - deadband` is the cut-in temperature. The heater remains on until
the sensing location reaches the setpoint
([EnergyPlus water-heater control](https://bigladdersoftware.com/epx/docs/22-2/input-output-reference/group-water-heaters.html)).
Consequently, a target of 53 degrees C and a 9 K hysteresis mean a nominal
start at 44 degrees C and stop at 53 degrees C. Small overshoot is compatible
with thermal inertia and sensor placement.

An active cycle must therefore be forecast as a finite recovery process. Its
remaining duration should decrease as elapsed runtime and tank temperature
increase. Re-running a forecast must not restart a fixed look-ahead duration.

### 3. Draw-offs are events with strong calendar structure and uncertainty

European water-heater test profiles explicitly define DHW demand as a sequence
of timed draw-offs with flow, temperature and energy requirements rather than
as a smooth space-heating load
([Commission Regulation (EU) No 814/2013, Annex III](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32013R0814)).
Measured-household research likewise models individual draw events by
time-of-day clusters and distributions of start time, flow and volume. Season,
weekday and household-specific behavior matter
([Ritchie et al., 2021](https://doi.org/10.1016/j.enbuild.2021.110727),
[Ritchie et al., 2021, control evaluation](https://doi.org/10.3390/en14071963)).

Single-home demand remains intrinsically uncertain. Accuracy improves with
aggregation, while individual dwellings retain substantial variability
([Pena-Bello et al., 2021](https://doi.org/10.1016/j.energy.2021.120658)). A
deterministic median profile is therefore useful, but calibrated quantiles or
event probabilities are a better long-term representation than pretending the
next draw is known exactly.

### 4. Outdoor temperature has a secondary, specific role

Outdoor temperature should not be treated as the direct driver of DHW event
timing. It can affect:

- heat-pump capacity and COP through the actual evaporator inlet-air condition;
- recovery duration and electrical energy per degree of tank recovery;
- mains-water temperature and tank/distribution losses over longer horizons.

The relevant source temperature is installation-dependent: EnergyPlus permits
HPWH inlet air from a room, outdoors or a schedule, and evaluates capacity and
operation from those inlet conditions
([EnergyPlus HPWH model](https://energyplus.net/assets/nrel_custom/pdfs/pdfs_v24.1.0/EngineeringReference.pdf)).
Outdoor air temperature is therefore a performance feature only when it is a
reasonable proxy for the heat pump's actual source air. It is not analogous to
the outdoor-temperature feature used for space-heating demand.

### 5. MPC separates state estimation, demand prediction and control

Published DHW MPC systems combine three distinct concerns:

1. estimate the current thermal state of the tank;
2. forecast future hot-water draws;
3. optimize heating while enforcing comfort/temperature constraints.

Receding-horizon feedback corrects forecast errors from measured tank state.
Research using real homes or experimental heat-pump systems reports that this
structure can shift operation and reduce cost while preserving hot-water
availability
([Maltais and Gosselin, 2022](https://doi.org/10.1016/j.ecmx.2022.100254),
[Baumann et al., 2023](https://doi.org/10.1016/j.enbuild.2023.112923),
[Premer et al., 2026](https://doi.org/10.1016/j.enconman.2025.120723)). Forecast
errors chiefly matter when they prevent the controller from preparing before a
large draw; closed-loop state feedback limits their persistence.

## Recommended Repository Model

Implement a dedicated `heat_pump_dhw` forecaster that remains independent of
`heat_pump_space_heating`.

### Inputs

Use normalized semantic inputs already available where possible:

- tank temperature, target and hysteresis;
- DHW charging state and measured electrical power;
- charging-allowed windows;
- circulation state;
- timestamp, weekday/day type and season;
- source-air or outdoor temperature as a conditional performance feature.

If later available, DHW flow, a second tank temperature or mains-water
temperature can improve state estimation without changing the model boundary.

### Learned cycle records

Extract completed DHW cycles from the Feature Store. For each cycle retain:

- start and end time, elapsed duration and electrical energy;
- start, intermediate and end tank temperature;
- time-of-day/day type, allowed-window position and circulation state;
- source-temperature context when trustworthy;
- interruptions, draws during recovery and data-quality flags.

Fit robust empirical relationships before considering a complex ML model:

- expected total cycle energy from starting thermal deficit;
- expected remaining energy and duration conditional on current temperature
  and elapsed cycle time;
- a normalized within-cycle power/energy shape;
- uncertainty quantiles once enough comparable cycles have matured.

### Active-cycle forecast

When DHW charging is active:

1. identify and retain the cycle start rather than starting a new synthetic
   cycle at every planning run;
2. estimate remaining energy and duration from the current temperature,
   elapsed time and learned completed cycles;
3. distribute only that remaining energy across the current and subsequent
   slots, clipped by the configured allowed window;
4. update the estimate from new observations, but never extend it merely
   because another forecast run occurred;
5. end the cycle forecast immediately when charging stops or the cut-out state
   is reached.

A new draw or a real decrease in estimated thermal state may legitimately
increase remaining energy. An unchanged snapshot may not.

### Future-cycle forecast

When DHW charging is inactive, forecast expected future energy from:

- learned cycle-start probability by quarter-hour, day type and season;
- current thermal distance to the 44 degrees C cut-in threshold;
- expected draw-off timing and size;
- circulation and allowed-window schedules.

The slot forecast should be probability-weighted expected energy, with
quantiles added after calibration. Current electrical power must not be copied
into future slots.

### Deliberate simplifications

- Start with an empirical equivalent-energy/two-state model, not a detailed
  stratified tank simulation.
- Do not infer DHW event timing directly from outdoor temperature.
- Do not introduce a neural network until cycle-level backtests show that the
  robust empirical model is inadequate.
- Preserve a history-only fallback for missing or stale tank context.

## Repository Impact

The current logic in
`custom_components/battery_strategy/forecasting/components.py` copies active
DHW power into every target up to 0.75 hours ahead. Because the horizon is
measured from each new request, the predicted end moves forward on every run.
This rolling extension contradicts both thermostat-cycle physics and the
repository's historical slot evidence.

The correction belongs in forecasting:

- replace the fixed active-power continuation with retained cycle state and a
  learned remaining-energy estimate;
- keep component history and learning in the Feature Store;
- keep space-heating logic separate;
- leave optimization, plan compilation and live control unchanged unless a
  later impact analysis finds a contract deficiency.

The existing forecasting contract already carries the required semantic
features and permits independent load components. An implementation can remain
contract-compatible if cycle state is internal persisted model state. Adding
new public uncertainty or state fields would require the repository's normal
contract impact analysis and approval.

## Acceptance Tests for a Later Implementation

- Repeated forecast runs with identical observations do not move the predicted
  cycle end or increase remaining DHW energy.
- During a normal uninterrupted cycle, predicted remaining energy declines and
  reaches zero when charging stops.
- A real draw or falling thermal-state estimate may increase remaining energy,
  with an explicit diagnostic reason.
- Completed historical cycles reproduce start time, duration and energy within
  reported error bands in walk-forward tests.
- Inactive forecasts follow learned event probabilities and allowed windows,
  not current power.
- Changing DHW logic cannot alter PV or space-heating forecasts.
- The optimizer receives only the corrected DHW slot-energy series; compiler
  and live-control outputs remain deterministic for an unchanged plan.


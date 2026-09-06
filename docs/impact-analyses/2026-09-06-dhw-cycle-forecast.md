# DHW Cycle Forecast Correction

Status: Implemented locally, pending release observation

## Reason and evidence

The former hot-water override copied current compressor power through every
target up to 45 minutes from each new forecast request. Rolling replans therefore
moved the predicted cycle end forward even while the real tank approached its
cut-out temperature. Historical feature slots show finite, recurring recovery
cycles and contain the temperature, hysteresis, charging-state and source-
temperature evidence needed for a bounded model.

The same heat pump cannot serve domestic hot water and space heating at once.
Historical space-heating profiles already contain the normal post-hot-water
buffer recovery, but an unexpectedly longer hot-water cycle must defer any
additional displaced heating demand.

## Semantic impact

No interface contract changes. Existing normalized features retain their
approved meanings:

- `dhw_target_c` is the thermostat cut-out target;
- `dhw_differential_c` is the positive hysteresis width;
- cut-in is `dhw_target_c - dhw_differential_c`;
- `outdoor_temperature_c` is a performance context, not a draw trigger.

The load forecaster implementation changes from fixed power persistence to a
finite empirical cycle. Domestic-hot-water and space-heating outputs remain
separate forecast components.

## Dependency and decision impact

The Home Assistant adapter continues to supply the same contract values. The
forecast composer delegates both related heat-pump series to one internal deep
module so it can enforce the shared-compressor constraint. PV forecasting and
unrelated load components are unchanged.

Corrected load slots can change optimizer plans and commercial discharge
budgets. Optimization, plan compilation, live control and actuation contracts
and implementations are unchanged; they continue to consume the forecast and
plan supplied to them.

## Compatibility

No feature-store schema migration is required. Historical slots remain valid.
Existing configurations must map the target role to the effective cut-out
temperature, not to a lower operating threshold. The config-flow discovery
already prefers an effective hot-water setpoint source. A versioned EMS-ESP
migration replaces the known lower-threshold role only when exactly one matching
effective-setpoint entity exists and its value equals lower threshold plus
hysteresis. If source states are not restored yet, matching Entity Registry
device ownership provides the same bounded verification. Setup retries the
correction so config-entry migration order cannot permanently skip it.
Ambiguous mappings remain unchanged and require reconfiguration.

Heat-pump state values are deliberately not declared stale merely because their
numeric state has not changed. Home Assistant state timestamps measure changes,
not source observations, and stable tank temperature, outdoor temperature or
activity can legitimately remain constant. A future freshness rule requires an
adapter-level observation or heartbeat timestamp; inventing one in the
forecaster would incorrectly suppress valid evidence.

Deferred space-heating demand is recovered only inside the requested forecast
horizon. This follows the existing forecast contract: energy beyond the horizon
is outside the returned product, not silently moved into a physically infeasible
slot. The normal 48-hour horizon is substantially longer than observed buffer
recovery, and regression tests verify conservation whenever recovery capacity
exists within it.

## Verification

- repeated active-cycle forecasts do not create a rolling 45-minute tail;
- a cycle first observed inside a slot remains bounded to that slot until the
  boundary supplies finalized energy evidence;
- remaining energy falls with decreasing remaining temperature deficit;
- source-temperature similarity influences cycle selection;
- extra hot-water occupation defers rather than deletes space-heating energy;
- component sums and load/PV isolation remain contract-valid;
- the recorded failure is replayed with no fabricated later hot-water slots.

## Rollout and rollback

Release as a forecast-only change. Before deployment, verify the configured
target source reports the effective cut-out value. Observe hot-water cycle-end
error, slot-energy error and resulting budget stability. Rollback restores the
previous forecast module only; execution-control state and hardware control do
not require migration.

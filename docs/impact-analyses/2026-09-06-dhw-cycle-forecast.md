# DHW Cycle Forecast Correction

Status: Implemented locally, pending release observation

## Reason and evidence

The former hot-water override copied current compressor power through every
target up to 45 minutes from each new forecast request. Rolling replans therefore
moved the predicted cycle end forward even while the real tank approached its
cut-out temperature. Historical feature slots show finite, recurring recovery
cycles and contain the temperature, hysteresis, charging-state and source-
temperature evidence needed for a bounded model.

The first implementation then over-corrected this risk: a cycle first observed
inside an unfinished slot was capped to that slot until finalized energy became
available. Production replay showed a valid uninterrupted cycle consequently
losing its future tail for one forecast vintage at the slot boundary. The model
now publishes the full thermally estimated tail immediately and deducts
estimated unfinalized energy since the charging-state or hot-water activity
transition on repeated runs. A missing transition timestamp fails
conservatively by deducting no unfinalized energy until a finalized active slot
exists.

The same heat pump cannot serve domestic hot water and space heating at once.
Historical space-heating profiles already contain the normal post-hot-water
buffer recovery, but an unexpectedly longer hot-water cycle must defer any
additional displaced heating demand.

The inactive-cycle follow-up keeps the historical cycle as its long-range
prior and refines only the next event. Comparable continuous historical states
are selected by tank temperature, circulation state, local time and day type.
The median observed time from the three nearest independent cycles supplies the
correction. A one-slot difference is deliberately retained as the prior because
it is within the forecast grid's resolution. The correction may move only the
next cycle, must respect its allowed start window and cannot overlap a later
prior cycle. Gaps never bridge an inactive observation to a later cycle. A fully
recursive tank-loss rollout was rejected after historical walk-forward
evaluation produced additional missed cycles.

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
- a cycle first observed inside a slot immediately includes its thermally
  estimated future tail without moving that tail on repeated forecasts;
- historical target changes or corrected lower-threshold mappings cannot be
  interpreted as thermal overshoot beyond the current cut-out;
- remaining energy falls with decreasing remaining temperature deficit;
- source-temperature similarity influences cycle selection;
- extra hot-water occupation defers rather than deletes space-heating energy;
- component sums and load/PV isolation remain contract-valid;
- the recorded failure is replayed with no fabricated later hot-water slots.

The inactive-cycle candidate was evaluated against RC18 on all 22 usable
retained cycles at one-, two- and four-hour lead times. Mean start-time MAE fell
from 21.36 to 20.23 minutes (5.3%), and mean eight-hour slot-energy MAE fell
from 0.02825 to 0.02796 kWh (1.0%). Both versions missed zero cycles. These
figures are release evidence for this retained-history snapshot, not a claim of
general model accuracy; production forecast-vintage monitoring remains the
longer-term check.

## Rollout and rollback

Release as a forecast-only change. Before deployment, verify the configured
target source reports the effective cut-out value. Observe hot-water cycle-end
error, slot-energy error and resulting budget stability. Rollback restores the
previous forecast module only; execution-control state and hardware control do
not require migration.

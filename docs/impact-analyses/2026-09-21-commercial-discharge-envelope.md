# Commercial Discharge Envelope

Status: implemented for the deterministic fallback and stochastic cutover
optimizer; no contract change.

## Problem

The deterministic fallback derived discharge permission from planned load,
future planned charge and explicit reservation heuristics. The stochastic
optimizer evaluated permission as expected consumed energy. Both approaches
could cap permission at current forecast load even when more discharge would
be commercially safe against unexpected eligible household demand.

## Decision

Both optimizers use the same continuation-value rule for discharge permission:

- expected execution remains forecast-dependent;
- permission assumes eligible live demand exists up to the candidate amount;
- current avoided import, throughput margin and the future value of inventory
  determine whether the candidate is safe;
- candidates are accepted only as one contiguous prefix up to the physical
  slot and SoC limits;
- unused permission has no economic or throughput cost;
- required grid charge and discharge permission remain mutually exclusive.
- forecast PV charging may coexist with discharge permission because live PV
  surplus takes precedence while the budget covers unexpected eligible load.

The deterministic fallback retains its existing plan and legacy feasibility
guards until cutover. Its historical discharge floor may be crossed only when
the same continuation model proves that lower inventory prevents future PV
export. The stochastic optimizer does not inherit that floor or the removed PV
recovery heuristics.

## Impact

Forecasting, scenario generation, compiler, live control, actuation and public
contracts are unchanged. The compiler continues to consume commercial
permission and live control continues to cap actual discharge at eligible
measured demand without grid export.

## Verification

- identical future state produces the same budget for different current-load
  forecasts while expected discharge remains load-dependent;
- later high-value demand is protected when no economic recharge exists;
- probability-one scenario input matches the P50 fallback;
- required-charge replay remains exact;
- PV-headroom and legacy-floor regression cases remain covered;
- a 48-hour horizon remains below the five-second optimizer runtime gate.

## Recovery

The pre-change release is tagged
`recovery/rc30-before-commercial-budget-envelope` at commit `b0d43c1`.

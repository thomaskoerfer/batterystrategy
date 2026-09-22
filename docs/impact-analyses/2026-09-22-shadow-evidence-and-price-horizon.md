# Shadow Evidence and Rolling Price Horizon

Status: **Approved** on 2026-09-22 by the owner's instruction to combine the
shadow cleanup with correction of the price horizon on both rollout branches.

## Decision

Keep the production plan/compiler/live contracts unchanged. Strengthen the
non-authoritative scenario pipeline in two related places:

1. Scenario evidence distinguishes unavailable historical weeks from damaged
   evidence. Short, bounded restart gaps may be repaired per channel. Continuous
   load, PV and price templates use linear interpolation; EV gaps only classify
   an unambiguous active/inactive state and never interpolate metered energy.
2. Planning uses a fixed rolling slot horizon. Published retail prices remain
   firm per slot. EEX Base/Peak fills only unpublished slots and is an uncertain
  anchor for joint scenario price paths, not a firm dispatch price.

Projected value is published for the rolling horizon and split into firm-price
savings and uncertain continuation value. Calendar-day views remain available
for display, but they are not the stable cross-midnight decision metric.

`ScenarioBuildRequest.market` and the optional price fields on `ScenarioSlot`
are additive contract changes approved with this decision. Firm slots are
identical in every path. Proxy slots retain the EEX intraday anchor and add the
historical price deviation from the selected joint load/PV/EV week. This keeps
dependence in one scenario rather than constructing independent price paths.

## Boundary impact

- Forecasting remains owner of marginal load, PV and EV distributions.
- Market Context remains owner of firm/proxy classification and the rolling
  horizon.
- Scenario Generation may consume normalized market observations solely to
  create joint uncertain paths; it does not make commercial decisions.
- Optimization consumes those paths and enforces physical first-slot charge
  and discharge headroom before constructing an executable decision.
- Compiler and Live Control are unchanged.
- Shadow evaluation is separated from trace persistence so diagnostics identify
  `insufficient_evidence`, `optimizer_failed` and storage failures distinctly.

## Compatibility and rollback

The extension is optional: an empty market tuple preserves point-price behavior
for tests and callers not yet providing market evidence. The shadow branch is
non-authoritative. The cutover branch receives the same contracts and model;
its existing orchestration difference remains the only authority difference.
Rollback is branch-local and requires no state migration.

## Verification

Regression coverage requires bounded multi-slot repair, non-interpolated EV
energy, partial firm-day precedence, constant rolling horizon across midnight,
first-slot battery headroom and separation of shadow evaluation from storage.
The complete test, lint, HACS and hassfest suites must pass on both branches.

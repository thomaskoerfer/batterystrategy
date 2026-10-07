# Rolling market valuation and full-day cost projection

Status: Approved by owner on 2026-10-07.

## Decision

Market context extends its information window from the 48-hour executable
horizon by a 24-hour non-executable continuation. Commercial policy uses only
that rolling information window. Calendar `today` and `tomorrow` labels remain
presentation groupings and cannot alter terminal value or discharge floor.

EEX delivery-day proxies are stable for one information vintage and survive a
restart in existing bounded market state. A vintage changes when normalized
Base/Peak settlement inputs or the proxy algorithm version changes. Firm retail
prices retain slot-level precedence.

The existing Today cost entities change meaning from remaining-plan cost to a
full end-of-day projection. They combine finalized EV-free actual slots through
one cutoff with plan slots after that cutoff. Tomorrow remains a full plan-only
day. Actual savings entities remain measured-only.

## Contract impact

Forecast, scenario, optimizer, compiler and live-control data structures are
unchanged. `CommercialPolicy` is unchanged; only its market-owned construction
semantics are corrected. Existing cost entity IDs are retained, while their
approved presentation meaning becomes full-day EoD projection.

## Safety and rollback

Continuation slots cannot enter the executable optimizer grid. Daily economics
is constructed after planning and cannot feed control. Rollback restores the
previous market enrichment and remaining-plan display without a state-schema
migration; additive market-cache keys are safely ignored by older code.

## Verification

- same EEX vintage produces the same shared delivery-day slots across midnight;
- changed EEX fingerprint permits a rebuild and firm slots still override;
- commercial policy is invariant to calendar rollover for unchanged rolling
  information;
- current-day actual and plan slots do not overlap and EV is excluded;
- tomorrow remains plan-only and measured savings remain actual-only.

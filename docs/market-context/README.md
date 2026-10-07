# Market context

## Purpose

Market context normalizes tariff slots, fills unpublished slots in a fixed
rolling horizon from wholesale context and derives commercial price metadata. It
does not forecast demand or PV and it never invokes the optimizer.

## Inputs and outputs

Inputs are slot-aligned retail prices, retained price history, optional
wholesale day products, timezone and commercial configuration. Outputs are an
enriched 48-hour executable horizon, a separate 24-hour continuation horizon
and terminal-value and discharge-floor inputs. Continuation prices value the
inventory boundary only; they never create executable plan slots.

Provider access and bounded caching stay inside this component. Real published
retail prices are never replaced, including a partially published day. EEX
Base/Peak values are marked proxy observations and passed to scenario generation
as uncertain anchors rather than silently promoted to firm prices. Load, PV, SoC and hardware state cannot
influence enrichment. The current adapter supports quarter-hour retail prices
and optional EEX day base/peak products without exposing either provider to the
optimizer contract.

Each EEX delivery-day proxy is retained by information vintage. The curve is
reused across midnight and restarts while the normalized Base/Peak settlement
fingerprint and proxy-model version are unchanged. Complete firm retail days
are retained as bounded shape evidence. New firm slots override proxy slots
immediately.

## Setup independence

Provider data is normalized before it leaves this boundary. Downstream code
depends on timestamped prices and commercial metadata, not account identifiers,
locations or provider-specific payloads. Additional tariff or wholesale sources
must implement the same normalized roles without changing optimization.

Operator projection exposes the last contiguous firm-price boundary and splits
rolling-horizon savings into firm and continuation portions. Calendar-day cost
cards are presentation slices and must not be interpreted as horizon value.
The current-day card combines finalized actuals with the remaining plan; it is
not an optimizer input.

## Verification

Tests cover real-price precedence, vintage stability across midnight and
restart, complete proxy grids, timezone alignment, rolling-policy invariance,
intraday shape and optional-provider failure containment.

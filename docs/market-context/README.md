# Market context

## Purpose

Market context normalizes tariff slots, fills unpublished slots in a fixed
rolling horizon from wholesale context and derives commercial price metadata. It
does not forecast demand or PV and it never invokes the optimizer.

## Inputs and outputs

Inputs are slot-aligned retail prices, retained price history, optional
wholesale day products, timezone and commercial configuration. Outputs are an
enriched price horizon plus terminal-value and discharge-floor inputs.

Provider access and bounded caching stay inside this component. Real published
retail prices are never replaced, including a partially published day. EEX
Base/Peak values are marked proxy observations and passed to scenario generation
as uncertain anchors rather than silently promoted to firm prices. Load, PV, SoC and hardware state cannot
influence enrichment. The current adapter supports quarter-hour retail prices
and optional EEX day base/peak products without exposing either provider to the
optimizer contract.

## Setup independence

Provider data is normalized before it leaves this boundary. Downstream code
depends on timestamped prices and commercial metadata, not account identifiers,
locations or provider-specific payloads. Additional tariff or wholesale sources
must implement the same normalized roles without changing optimization.

## Verification

Tests cover real-price precedence, complete proxy grids, timezone alignment,
intraday shape and optional-provider failure containment.

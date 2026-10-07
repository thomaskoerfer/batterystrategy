# Daily-economics agent rules

Read the public README, root architecture and interface contracts before
changing this component.

## Allowed

Combine finalized normalized actual slots and future operator-plan slots into
non-authoritative calendar-day cost values.

## Forbidden

Do not fetch Home Assistant state, alter a forecast or plan, recreate measured
savings, invoke the optimizer, compile permissions or influence live control.
Never overlap actual and planned intervals.

## Required checks

Run cutoff, EV-exclusion, presentation-entity and architecture-boundary tests.
Changing cost semantics requires an approved impact analysis.

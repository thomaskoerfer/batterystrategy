# Market-context agent rules

Read the public README, root architecture, interface contracts and parent
guidance before changing this component.

Own provider access, bounded caching, price normalization, missing-day
enrichment and commercial price context. Do not import forecasting or optimizer
implementations, inspect battery state, create plans or influence live commands.
Never replace complete published prices with estimates.

Run proxy, timezone, boundary and failure-containment tests. Changes to policy
semantics require impact analysis and explicit owner approval even when no data
structure changes.

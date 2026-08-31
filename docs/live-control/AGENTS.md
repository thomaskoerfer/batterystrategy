# Live-control agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before changing live behavior. Decision precedence and EV/manual policy
are owner-approved and normative.

## Allowed

Own fast meter following, policy precedence, EV treatment, PV-follow, budget
consumption, stale-input safety, smoothing and generic command generation.

## Forbidden

Do not optimize prices, retrain forecasts, mutate future plans or translate
commands into vendor entities. Price-sensitive discharge requires permission;
load-following discharge does not.

## Required checks

Cover precedence, every EV-policy combination, both discharge modes, PV-follow,
manual override, required charge, stale inputs, disabled control and trace
separation between plan permission and live response.

## Setup independence

Use normalized snapshots and policies, never concrete entities, devices or
private connection details.

# Planning-service agent rules

Read the public README, root architecture, interface contracts and parent
guidance before changing orchestration.

## Allowed

Compose explicit market, forecast, battery and configuration inputs, invoke the
pure optimizer and return its canonical `BatteryPlan` with separate
typed operator points, typed daily costs and non-authoritative publication
metadata. Build fresh projections from typed plan values, never by parsing the
publication dictionary.

## Forbidden

Do not fetch provider data, build forecasts, read persistence, calculate live
power, write hardware or maintain a second optimizer path. Do not add economics
to publication or reconstruct executable intent from profile data.

## Required checks

Run optimizer scenarios, publication parity, boundary and architecture tests.
Changes in units, signs, alignment, budgets, source allocation or SoC semantics
require impact analysis and explicit owner approval.

## Setup independence

Use contract objects and normalized capability settings. Never add entity IDs,
vendor commands, private locations or household-specific policy branches.

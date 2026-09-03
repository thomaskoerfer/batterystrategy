# Measured-savings agent rules

Read the public README, root architecture, interface contracts and parent
guidance before changing this ledger.

## Allowed

Own actual counter-delta accounting, price attribution, bounded retention and
reported economics.

## Forbidden

Do not feed accounting back into forecasting, optimization or live commands
without an approved contract and impact analysis. Do not infer missing prices
as zero or advance counters while prices are absent.

## Required checks

Run attribution, restart, missing-data, retention and cumulative-total tests.
Changing the meaning of a published savings metric requires impact analysis
and explicit owner approval.

## Setup independence

Consume normalized measurements only. Never add private tariff accounts,
installation identifiers, vendor payloads or recorder-schema dependencies.

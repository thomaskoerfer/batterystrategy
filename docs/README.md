# Architecture layer documentation

Battery Strategy is organized around explicit architecture layers. Each layer
has a public README describing its stable responsibility and an `AGENTS.md`
describing how coding agents must work in that boundary.

| Layer | Public guide |
| --- | --- |
| Data adapters and feature store | [README](data-feature-store/README.md) |
| Forecasting | [README](forecasting/README.md) |
| Market context | [README](market-context/README.md) |
| Optimization | [README](optimization/README.md) |
| Planning service | [README](planning-service/README.md) |
| Plan compiler | [README](plan-compiler/README.md) |
| Live control | [README](live-control/README.md) |
| Actuation | [README](actuation/README.md) |
| Measured savings | [README](savings/README.md) |
| Evaluation and diagnostics | [README](evaluation/README.md) |

The root [architecture](../ARCHITECTURE.md) defines the data flow. The
[interface contracts](../INTERFACE_CONTRACTS.md) are normative. Layer guides
explain those boundaries without depending on one Home Assistant installation.

## Documentation rule

A migration phase is incomplete until every affected layer guide and agent file
matches the implementation. Public layer documentation may list currently
supported provider or device classes, but it must not contain installation
addresses, entity IDs, hostnames, URLs, serial numbers, credentials, filesystem
paths or household-specific labels.

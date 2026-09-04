# Architecture component documentation

Battery Strategy has five production layers. Detailed component guides document
the sub-boundaries and supporting concerns within that design. Each component
has a public README describing its stable responsibility and an `AGENTS.md`
describing how coding agents must work in that boundary.

| Production layer | Components |
| --- | --- |
| Data and feature store | Data adapters, normalized observations and feature store |
| Forecasting | Load, PV and component forecasting |
| Optimization | Pure economic optimizer |
| Execution control | Plan compiler and live control |
| Actuation | Hardware command adapter |

Market context is an input adapter. Planning service and planning runtime
orchestrate the five layers. Savings, evaluation and diagnostics are
non-authoritative observers. These are supporting components, not additional
production layers.

| Component | Public guide |
| --- | --- |
| Data adapters and feature store | [README](data-feature-store/README.md) |
| Forecasting | [README](forecasting/README.md) |
| Market context | [README](market-context/README.md) |
| Optimization | [README](optimization/README.md) |
| Planning service | [README](planning-service/README.md) |
| Planning runtime | [README](planning-runtime/README.md) |
| Plan compiler | [README](plan-compiler/README.md) |
| Live control | [README](live-control/README.md) |
| Actuation | [README](actuation/README.md) |
| Measured savings | [README](savings/README.md) |
| Evaluation and diagnostics | [README](evaluation/README.md) |

The root [architecture](../ARCHITECTURE.md) defines the data flow. The
[interface contracts](../INTERFACE_CONTRACTS.md) are normative. Component guides
explain those boundaries without depending on one Home Assistant installation.

## Documentation rule

A change is incomplete until every affected component guide and agent file
matches the implementation. Public component documentation may list currently
supported provider or device classes, but it must not contain installation
addresses, entity IDs, hostnames, URLs, serial numbers, credentials, filesystem
paths or household-specific labels.

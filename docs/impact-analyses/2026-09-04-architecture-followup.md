# Architecture follow-up

## Scope

This follow-up evaluates four opportunities identified after the Phase 7
architecture cleanup. It must preserve the approved forecast, optimization,
plan, compiler, live-control and actuator contracts.

## Decisions

### 1. Active-slot compiler runtime: implement

The coordinator previously owned compiler construction, commitment state,
measured slot throughput and restart restoration directly. Those concerns now
belong to `PlanCompilerRuntime`. The coordinator still owns Home Assistant
lifecycle, refresh scheduling, persistence I/O and the sole actuator call.

This is an internal ownership change. Published directives and persistence
payloads are unchanged. A behavioral coordinator-cycle test verifies the order
of accounting, planning, slot synchronization, compilation and persistence.

### 2. Typed planning output: defer

Replacing the established planning mapping with a typed result would improve
local discoverability, but it would also affect persistence and multiple public
projection consumers. The benefit does not currently justify that migration.

Any future implementation requires a separate impact analysis and explicit
approval for every affected interface contract. An adapter must protect stored
state and entity behavior during migration.

### 3. Live-control model convergence: defer

The current model boundary contains some conversion between planning-facing
and actuator-facing command types. Collapsing those types now would touch the
active safety path and may change approved command semantics. The types remain
separate until a concrete simplification can be demonstrated with parity and
restart tests.

Any future convergence requires a separate impact analysis and explicit
contract approval. It must retain single-writer actuation, fail-closed behavior,
EV policy, PV-follow precedence and disabled one-shot zeroing.

### 4. Configuration knowledge: implement partially

Option defaults and numeric constraints now have one definition module.
Profile-aware entity and load-component validation now has one validation
module. The config flow remains responsible only for navigation and form
construction; number, switch, select, coordinator and actuator consumers reuse
the shared definitions.

Historic form constraints that intentionally differ from runtime entity ranges
remain explicit in the shared definition. This avoids an accidental user-facing
behavior change while removing duplicated literals.

## Contract impact

No normative contract or contract schema changes are made. In particular,
`INTERFACE_CONTRACTS.md` and the contract model package remain unchanged.

## Verification

- Full Python and Home Assistant test suite.
- Lint and import-order checks.
- Compiler-runtime restart and fail-closed tests.
- Behavioral coordinator-cycle ordering test.
- Configuration default and form-constraint regression tests.

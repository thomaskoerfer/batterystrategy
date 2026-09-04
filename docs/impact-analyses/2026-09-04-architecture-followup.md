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

### 2. Typed planning output: implement

The owner approved this contract change on 2026-09-04. The planning pipeline now
returns an immutable `PlanningResult` containing the optimizer's canonical
`BatteryPlan`, an immutable `StrategyPlan` operator projection and a separate
diagnostic/profile mapping. The compiler receives only the canonical plan;
production no longer contains a `StrategyPlan`/profile-to-`BatteryPlan`
reconstruction path.

Planning persistence advances from schema 9 to schema 10 and stores the
canonical plan through one codec. Existing schema-9 operator data remains
visible, but is deliberately non-executable until a new optimizer result is
available. Invalid canonical data and a mismatch with current physical battery
constraints also fail closed. This is a one-time migration without a runtime
compatibility facade.

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

Recommendation 2 changes the planning-publication interface and persisted
planning schema as described above. `BatteryPlan`, optimizer, compiler,
live-control and actuator semantics are unchanged. The contract model package
does not change shape; `INTERFACE_CONTRACTS.md` now makes the canonical-plan and
operator-projection separation normative.

## Verification

- Full Python and Home Assistant test suite.
- Lint and import-order checks.
- Compiler-runtime restart and fail-closed tests.
- Behavioral coordinator-cycle ordering test.
- Configuration default and form-constraint regression tests.
- Canonical-plan persistence round trip, legacy display-only migration,
  malformed-state fail-closed and physical-constraint mismatch tests.
- Static guard proving no production profile-to-`BatteryPlan` reconstruction.

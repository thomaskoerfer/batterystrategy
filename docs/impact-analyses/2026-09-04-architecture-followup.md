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

Planning persistence advances from schema 9 to schema 11 and stores the
canonical plan plus its authorizing execution-policy fingerprint through one
codec. Existing schema-9/10 operator data remains
visible, but is deliberately non-executable until a new optimizer result is
available. Invalid canonical data and a mismatch with current physical battery
constraints or planning switches also fail closed. This is a one-time migration
without a runtime compatibility facade.

### 3. Live-control model convergence: implement

The owner approved this contract change on 2026-09-04. Home Assistant values
are normalized once into `LiveMeasurements`; the compiler emits the contract
`PlanLiveDirective` directly; and `DeterministicLiveController` returns one
`LiveControlResult` containing an actuator-ready `BatteryCommand`, explicit
`LiveControlState` and separate `LiveDiagnostics`. The removed integration
input, directive and diagnostic-command models had no remaining owner.

`required_charge_power_w` makes the compiler's latched required-rate intent
explicit rather than reconstructing it from an operator profile. Manual
physical power limits are explicit in `LivePolicy`, because manual control may
legitimately operate while the economic directive is closed. These are
coordinated in-memory changes. The subsequent safety review added the schema-11
policy fingerprint; no entity IDs change.

Direction hysteresis now consumes and returns explicit `LiveControlState`.
Single-writer actuation, fail-closed safety, EV policy, PV-follow precedence,
required-charge behavior, disabled one-shot zeroing and dashboard values remain
covered by parity regression tests.

The independent architecture and Home Assistant reviews also tightened existing
semantics without adding commercial behavior: the runtime selects the current
plan slot atomically and carries measured energy across slot boundaries; stale
grid, battery, SoC and policy-relevant EV inputs fail closed; fail-safe zero
writes are retried until device state confirms them; manual control remains
independent of economic slot validity; optimizer completion publishes
immediately; and unload is refused unless an active battery can first be stopped.

### 4. Configuration knowledge: implement partially

Option defaults and numeric constraints now have one definition module.
Profile-aware entity and load-component validation now has one validation
module. The config flow remains responsible only for navigation and form
construction; number, switch, select, coordinator and actuator consumers reuse
the shared definitions.

Historic form constraints that intentionally differ from runtime entity ranges
remain explicit in the shared definition. This avoids an accidental user-facing
behavior change while removing duplicated literals.

### 5. Single captured planning time: implement

The adapter now captures `captured_at_ms` once with the live measurement
snapshot. The immutable `PlanningRuntime` requires it, and all planning-time
price selection, history windows, forecast requests, result restoration and
state ordering derive from that value. Recorder queries and their normalized
results are bounded at that instant. Monotonic time remains limited to the
adapter's cache age and is not a planning input.

### 6. Concrete forecast contract implementations: implement within contracts

Production forecast construction now uses independent configured
`LoadForecaster` and `PvForecaster` implementations and a composer that only
constructs `ForecastBundle`. The existing mathematical functions and per-run
inputs are unchanged, with exact bundle-parity coverage. The approved forecast
contracts are unchanged; moving calibration observations into their data model
would require a future owner-approved impact analysis.

### 7. Typed planning state ownership: implement without schema change

`PlanningStateStore` now owns loading, migration, typed owner-state projection,
serialization, startup hydration and atomic writes for the existing schema-11
document. Forecast learning, virtual simulation, market cache, measured savings
and publication mutate separate typed sections. A lifecycle lease and atomic
stale-run check prevent old coordinator generations or older results from
overwriting current state.

### 8. Fresh typed operator projection: implement

Fresh optimizer publication creates typed operator points and daily costs
directly from the canonical plan and aligned forecasts. The dictionary parser
is retained only for display hydration from persisted output. It cannot create
executable compiler permission; only the separately persisted canonical
`BatteryPlan` can do so.

### 9. Captured planning snapshot completion: implement internally

Architecture and Home Assistant reviews approved finishing the existing
captured-snapshot boundary without changing an approved interface contract.
`PlanningRuntime` now contains only immutable domain observations, role-keyed
bounded history, normalized tariffs and forecast inputs. Provider aliases,
units, current-price fallback behavior and Recorder entity resolution belong to
the Home Assistant adapter. `PlanningStateStore`, configuration paths and raw
state mappings cannot cross into the planning snapshot.

Follow-up review restored exact date-scoped current-price fallback and existing
zero-option normalization, preserved zero/negative future prices, canonicalized
provider tariff authority, and replaced the signed battery observation with
separate non-negative charge and discharge flows required by the existing
contract.

The adapter preserves the required lifecycle order: capture on the event loop,
read bounded Recorder history in the executor, load typed owner state, invoke
planning, persist that state under the existing schema-11 lease, then publish.
No optimizer, compiler, live-control, actuator, entity or persistence-schema
semantics changed. Existing EV-history scaling behavior was deliberately
preserved for this ownership-only refactor and remains separately testable debt.

## Contract impact

Recommendation 2 changes the planning-publication interface and persisted
planning schema as described above. Recommendation 3 changes the coordinated
in-memory compiler/live/actuator interfaces and binds restored intent to its
authorizing policy without changing operator entity IDs. `INTERFACE_CONTRACTS.md` defines both canonical-plan
separation and the direct live-control chain normatively.

Recommendations 5-9 do not change approved interface contracts, entity IDs,
schema-11 keys, optimizer behavior, plan-compiler semantics, live policy or the
single actuator path.

## Rollback

The pre-follow-up deployment remains `0.2.0-rc.6`. Reverting this candidate to
that release is safe because the persisted planning document remains schema 11
with unchanged keys and meanings. Successfully unload the integration before
replacing files; that unload explicitly revokes its state writer. If
unload cannot complete, restart Home Assistant before installing RC6. RC6 reads
the same canonical plan and operator data; if validation fails, control remains
closed until a fresh optimizer run. No reverse data migration is required.

## Verification

- Full Python and Home Assistant test suite.
- Lint and import-order checks.
- Compiler-runtime restart and fail-closed tests.
- Behavioral coordinator-cycle ordering test.
- Configuration default and form-constraint regression tests.
- Canonical-plan persistence round trip, legacy display-only migration,
  malformed-state fail-closed and physical-constraint mismatch tests.
- Static guard proving no production profile-to-`BatteryPlan` reconstruction.
- Current-slot rollover and cross-boundary energy-accounting tests.
- Stale numeric input, fail-safe retry and unload-race regression tests.
- Exact forecast-bundle parity through concrete load/PV contract owners.
- Single captured-time and quarter-boundary price tests.
- Typed state round trip, stale-run rejection and obsolete-lifecycle rejection.
- Fresh projection parity with a guard that forbids the restore parser.
- Active-slot restore continuity, captured Recorder upper bounds and stale-run
  retry suppression across option changes.

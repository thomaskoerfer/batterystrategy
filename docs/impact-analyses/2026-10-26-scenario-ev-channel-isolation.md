# Scenario EV channel isolation

Status: **Approved** on 2026-10-26 for implementation and shadow evaluation.

## Problem

The feature store and forecast contracts already keep EV-free house load, PV
generation and EV charging separate. Scenario admission nevertheless rejected
an entire historical weekly path when only its EV evidence was missing or its
first EV state did not match a currently active charge. Valid load/PV evidence
was therefore lost, reducing scenario-generation availability and coupling a
sparse, weakly predictable EV event to unrelated forecast channels.

## Decision

- Keep load/PV weekly analogues, component-distance weighting and bounded
  repair unchanged.
- Admit and weight a load/PV path without using EV quality or EV repair count.
- Retain same-path EV evidence when it is valid and compatible.
- Replace only an invalid EV template with the nearest eligible EV template
  selected directly from raw historical EV evidence before load/PV validation
  and the path cap.
- If no eligible historical EV template exists, construct only the EV channel
  from the current EV occurrence and energy marginals.
- Preserve deterministic replay and emit structured diagnostics for every
  EV-channel fallback.
- Start a fresh Scenario Builder model cohort so old and new shadow evidence is
  never mixed.
- Persist bounded Scenario Builder replay metadata beside forecast traces and
  rebuild history from the single retained feature-store artifact. Existing
  traces use an explicitly labelled quantile fallback, so an initial
  retrospective comparison does not require another live collection window.
- Label new replay output as reconstructed rather than exact because the
  feature store may receive later additive component enrichment. Fingerprints
  include the actually consumed weekly evidence and residual values so two
  different reconstructed inputs cannot share an identity.

## Contract impact

No executable or semantic interface contract changes. `HistoricalFeatureSlot`
continues to expose separate measured series, `ForecastDistributionBundle`
continues to carry separate load/PV/EV marginals, and `ScenarioBuildRequest`
and `ScenarioBuildResult` are unchanged. The change restores channel isolation
inside the Scenario Builder implementation.

## Safety and rollout

The stochastic optimizer remains evaluation-only. Production forecasting,
the authoritative optimizer, plan compiler, live controller and actuator are
unchanged. The new model identity deliberately resets the homogeneous shadow
cohort; cutover still requires the documented observation and perfect-foresight
gates.
Replay is an offline evaluation tool and is prohibited on the production Home
Assistant host. It reads immutable local copies sequentially and cannot reach
planning, compilation, live control or actuation.

## Verification

- Public-seam tests prove missing and state-incompatible EV evidence cannot
  reject otherwise valid load/PV paths.
- Existing tests continue to prove EV occurrence probability, deterministic
  replay, repair bounds and component-aware weighting.
- Full unit, formatting, architecture, HACS and hassfest checks must pass.
- Retained-history evaluation must run from an immutable local snapshot under
  local resource limits, never on the production server.

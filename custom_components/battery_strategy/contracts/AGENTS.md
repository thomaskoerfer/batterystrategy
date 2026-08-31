# Contract package rules

Contract types are the stable language between architecture layers.

- Keep them immutable, deterministic and independent of Home Assistant,
  persistence, network and provider libraries.
- Use explicit units, signs, timestamps, optionality and invariants.
- Do not add convenience fields that transfer ownership between layers.
- Any semantic, unit, required-field or invariant change needs an impact
  analysis, explicit owner approval and coordinated producer/consumer tests.
- Persisted schema changes need a version, migration and rollback path.
- Additive fields still require consumer-readiness analysis.
- Keep fixtures setup-neutral and free of private identifiers.

Run contract tests plus producer and consumer tests for every changed boundary.

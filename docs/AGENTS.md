# Architecture documentation rules

These instructions apply to all architecture-component documentation below this
directory.

## Required context

Read the root `ARCHITECTURE.md`, `INTERFACE_CONTRACTS.md`, this component's
`README.md` and any approved impact analysis before changing layer behavior.

## Documentation contract

- Keep each component README aligned with implementation and executable contracts.
- Describe purpose, inputs, outputs, non-responsibilities, supported capability
  classes and verification.
- Update README and agent guidance in the same change as an affected component.
- Treat contract semantics as binding. Changes require an impact analysis and
  explicit owner approval, even when the implementation would be easy.
- Keep historical design work in impact analyses; active guides describe only
  the production architecture and current limitations.

## Setup independence

Public documentation and committed agent guidance must not contain concrete
entity IDs, household labels, addresses, hostnames, URLs, serial numbers,
credentials, private provider payloads or local filesystem paths. Name a vendor
or provider only as a supported capability class. Use normalized roles and
generic examples.

## Required checks

Run architecture-documentation tests, contract tests and the tests owned by
every changed component. Check links and review the diff for private setup data.

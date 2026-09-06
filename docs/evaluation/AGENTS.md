# Evaluation and diagnostics agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before adding metrics, traces or backtests.

## Allowed

Observe immutable outputs and matured actuals; produce bounded forecast,
optimizer, savings and perfect-foresight diagnostics.

## Forbidden

Do not influence forecasts, plans or commands; do not retrain inside a fixed
evaluation window; do not acquire an actuator reference. Never store unbounded
attributes or expose private configuration in diagnostics.

Forecast-vintage capture may serialize only an already-produced immutable
`ForecastBundle`. Keep it outside Recorder, bounded by time and frequency, and
failure-contained after authoritative plan persistence. Evaluation results must
never be read by forecasting, optimization, compilation, live control or
actuation. A later learning or model-selection loop is a separate behavioral
change requiring impact analysis and review.

## Required checks

Test alignment, maturation, missing-data exclusion, retention, redaction,
non-authoritative flags and failure containment. Define observation duration and
tolerances before evaluating release readiness.

## Setup independence

Use contract fields and model versions, never household identifiers or private
connection details.

# Forecasting agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before working on this layer.

## Allowed

Own deterministic load and PV prediction, uncertainty, model quality and named
load-component composition from normalized feature and weather inputs. Keep
concrete `LoadForecaster` and `PvForecaster` implementations independent; their
composer may only invoke them and construct `ForecastBundle`.

## Forbidden

Do not read entities, recorder storage, files or networks. Do not use prices,
battery SoC, battery policy or optimizer state. Keep load and PV models and
their learned state independent. A component change must not alter unrelated
components.

## Required checks

Evaluate load and PV separately by lead time, bias, error, daily energy and
coverage. Test model isolation, missing component data and deterministic replay.

## Setup independence

Use semantic feature keys and capability classes, never concrete installation
identifiers or private endpoints.

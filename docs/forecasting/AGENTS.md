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

For heat pumps, keep domestic-hot-water recovery distinct from space-heating
demand. Model hot-water charging as a finite temperature-deficit cycle; never
extend current compressor power by restarting a rolling duration. Model the
shared-compressor and post-hot-water heating-recovery relationship inside the
heat-pump forecast implementation while continuing to publish separate load
components. Do not add weather features such as humidity without demonstrated
incremental walk-forward value.

## Setup independence

Use semantic feature keys and capability classes, never concrete installation
identifiers or private endpoints.

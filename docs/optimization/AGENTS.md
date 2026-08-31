# Optimization agent rules

Read `README.md`, the root architecture, interface contracts and parent agent
rules before working on this layer.

## Allowed

Own the pure economic objective, physical battery constraints, terminal value,
PV headroom, source allocation and commercial discharge permission.

## Forbidden

Do not access Home Assistant, persistence, weather, files, networks or the wall
clock. Do not perform live meter following or vendor translation. Do not encode
ranks or heuristics as fictional monetary value.

## Required checks

Run golden scenarios, retained-history replay and edge cases for efficiency,
uneconomic cycles, horizon boundaries, PV spill, scarce inventory, EV exclusion
and deterministic tie-breaking.

## Setup independence

Represent batteries, tariffs and providers only through normalized contracts;
never embed one installation's identifiers or connection details.

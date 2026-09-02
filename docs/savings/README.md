# Measured savings

Measured savings account for actual battery charge and discharge after the
fact. Counter deltas are stamped at the contemporaneous retail price and kept
in a bounded daily ledger with a permanent archived total. This component
cannot influence forecasts, plans, live policy or actuation.

Grid-sourced charge is a cost; PV-surplus charge has zero energy cost. Measured
battery output is currently credited gross at the retail import price. This is
an explicit product decision until a separately approved avoided-import metric
replaces it.

Inputs are normalized battery counters, grid flow, battery power and prices
supplied by adapters. Missing price data never advances the counter tracker.
Tests cover attribution, units, missing-price recovery, resets, restart gaps,
retention and lifetime stability.

# Data adapters and feature store

## Purpose

This layer converts provider-specific Home Assistant state into normalized,
quality-scored 15-minute facts. It is the only layer allowed to know entity
mappings, source units, recorder access or provider payload formats.

## Inputs and outputs

Inputs may include grid import/export, PV generation, battery charge/discharge,
EV charging, electricity prices, weather and independently metered loads.
Adapters normalize signs and units before data crosses the boundary.

The primary output is `HistoricalFeatureSlot`: one immutable UTC-aligned slot
containing energy values, optional named load components and explicit quality
metadata. Weather and market adapters return the normalized contract types used
by forecasting and optimization.

## Responsibilities

- validate configured sources and their measurement units;
- separate import/export and charge/discharge into non-negative flows;
- reconstruct EV-free household load without double-counting battery power;
- time-weight irregular measurements into quarter-hour energy;
- distinguish missing data from measured zero;
- persist compact, versioned features independently of the recorder backend;
- expose quality, retention and migration diagnostics.

This layer does not forecast, value energy, allocate a battery budget or issue a
hardware command.

## Persistence

The feature store keeps one compressed record per finalized slot with bounded
retention. Recorder access is limited to bootstrap, repair and backfill through
an adapter. Forecasting and optimization must behave identically whether the
Home Assistant recorder uses SQLite, MariaDB, PostgreSQL or another supported
backend.

Persisted schema changes require versioning, migration, rollback coverage and a
non-destructive data-quality check. Raw fast sensor updates do not belong in the
feature store.

## Current support

The integration currently accepts signed grid power, separate import/export or
three-phase grid measurements; a PV power source; battery SoC and power; an
optional EV meter; quarter-hour market data; and normalized weather. Supported
load-component profiles currently include a heat pump, shared-meter air
conditioning, finite cyclic appliances and a generic metered consumer.

When a cyclic appliance is newly configured, the adapter may seed only its
missing component energy from up to 21 days of Home Assistant Recorder power
history. Recorder states are normalized to watts and integrated as stepwise
measurements on the existing feature-store grid. Recorder persists state
transitions rather than periodic samples, so a valid state may be held within
the 15-minute slot being integrated. Existing component facts,
whole-house totals and other components are never overwritten. This bounded,
idempotent bootstrap prevents an established feature store from losing all
component-model readiness solely because a separately metered appliance was
added. If Recorder history is unavailable or too short, normal live collection
and the existing warm-up gate remain authoritative. The bootstrap starts only
after the coordinator and live tracking are available, is cancelled on unload,
and records a small HA-storage completion marker per component and anonymized
source mapping so reloads do not repeatedly scan the same range. Changing the
mapped source permits one new bootstrap. Explicit unavailable transitions
invalidate a slot; a confirmed zero may remain valid until it changes.

Backfill persistence performs the read, additive merge and atomic write under
one store lock. Concurrent live or repair data therefore remains authoritative;
the bootstrap may only add component facts that are still missing.

For an active heat-pump hot-water state, the adapter exposes the elapsed time
since Home Assistant's state transition as `dhw_active_age_s`. The configured
charging-state transition is preferred; the required hot-water activity
transition is the fallback. This value is cycle timing evidence, not a freshness
check: an unchanged available state remains valid. If neither transition is
available, the feature is omitted rather than synthesized.

Transient weather-provider failures may reuse the last successful snapshot for
the same grid for a bounded period. Reused slots are explicitly marked as
estimated; expired or incompatible snapshots remain missing.

These are supported source classes, not assumptions in downstream contracts.
New devices are mapped to the same normalized flows and feature keys.

## Setup independence

No code or public documentation in this layer may depend on a particular
address, entity ID, hostname, serial number, household label or local storage
path. Such values belong only to a user's config entry and runtime diagnostics,
where diagnostics must redact sensitive values.

## Verification

Tests cover unit/sign normalization, time-weighting, restart gaps, counter
resets, missing inputs, component reconciliation, schema migration, retention
and recorder independence. Weather tests also cover bounded stale-if-error
reuse, quality marking and expiry. A data-layer change must prove that forecast,
optimizer and live-command outputs remain unchanged unless a separately
approved downstream contract change is intended.

## Production status

The compressed feature store is the production forecast history source and the
bootstrap source for learned quarter-hour samples. Bounded dashboard and
measured-savings history is obtained through Home Assistant's public history
API in `history_adapter.py`; no recorder engine or table schema crosses into
forecasting or optimization.

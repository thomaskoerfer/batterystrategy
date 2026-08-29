# Battery Strategy

Battery Strategy plans and controls a Home Assistant battery using quarter-hour electricity prices, learned house load, PV forecasts and live grid measurements. EV charging can be excluded from automatic battery discharge.

Development and refactoring must follow the documented
[target architecture](ARCHITECTURE.md), especially its single-actuator safety
invariant and staged migration gates. Layer changes must also preserve the
[interface contracts](INTERFACE_CONTRACTS.md).

> [!WARNING]
> This integration can command real battery hardware. New installations start with control disabled. Verify entity mappings, units, signs, SoC limits and the displayed live command before enabling control. Keep the battery/BMS safety limits active.

## Status

This beta currently targets Germany, Tibber Prices quarter-hour data and Zendure-compatible AC input/output controls. Other batteries can be monitored with the generic profile, but automatic actuation requires a supported control profile.

## Installation

1. In HACS, add `https://github.com/thomaskoerfer/batterystrategy` as a custom repository of type **Integration**.
2. Install **Battery Strategy** and restart Home Assistant.
3. Add **Battery Strategy** under **Settings > Devices & services**.
4. Map the grid, PV, battery, EV and Tibber Prices chart-data entities.
5. Review strategy and battery limits. Control remains disabled until explicitly enabled.

## Required inputs

- Grid power as a signed sensor, three phase sensors, or separate import/export sensors
- Current PV power in W
- Battery SoC in percent
- Tibber Prices chart-data export entity with 15-minute records in its `data` attribute
- For Zendure control: AC mode, input limit and output limit entities

Optional inputs include EV charging power and detailed Zendure battery power entities. Power units are read from each entity's Home Assistant metadata; configure `W` or `kW` correctly at the source.

The selected grid-meter mode is authoritative. Battery Strategy never switches
automatically between grid sources: stale or unavailable grid measurements put
live actuation into fail-safe instead. Tibber Prices is used exclusively for
electricity prices and is never a fallback for grid power.

## Configuration

- **Mode:** enable control, PV charging, grid charging and discharge policy
- **EV policy:** choose whether PV serves the EV before battery charge-follow, exclude EV charging from discharge and optionally block all battery discharge while EV charging
- **Battery and prices:** usable SoC, capacity, power, RTE, margin, feed-in tariff and PV capacity
- **Manual override:** manual charge or discharge; the integration services optionally accept a duration

Existing installations preserve their stored control state. A fresh installation starts disabled.
Use **Reconfigure** to change mapped Home Assistant entities and **Configure**
to change strategy options. Both paths perform one managed integration reload.

Independently metered loads are added as **Load component** subentries on the
Battery Strategy integration. Available profiles are EMS-ESP heat pump (DHW and
space heating are separated), shared-meter air conditioning with multiple
indoor climate entities, and a generic metered consumer. These profiles feed the
recorder-independent feature-store forecast and remain isolated from PV and
other load components. Open-Meteo weather is loaded centrally from the Home
Assistant location and requires no weather entity.

## Example dashboard

The integration ships a complete Lovelace example at
`custom_components/battery_strategy/examples/lovelace_dashboard.json`. Paste its
JSON into a new dashboard's raw configuration editor after setup. A regression
test verifies that every Battery Strategy entity referenced by the dashboard is
provided by the same release.

## Safety behavior

- Live grid changes use Zendure-style event gating: a normal four-second cadence,
  significant-change updates after at least 2.2 seconds, a 5 W command tolerance
  and a 50 W idle-start threshold. The ten-second coordinator interval remains a
  fallback.
- Direction changes clear the opposite limit before switching AC mode and
  applying the new target. A short/long charge-restart guard prevents rapid
  discharge-to-charge oscillation.
- Stale or unavailable grid inputs zero both battery limits once and remain in fail-safe until measurements recover.
- A configured EV power sensor is bridged for three minutes. If it remains unavailable, automatic discharge is blocked whenever the EV policy excludes EV consumption; charging remains available.
- The EV switches remain authoritative in the live layer: discharge can be
  blocked completely while charging, or limited to non-EV household load so the
  battery does not supply the car.
- A persisted SoC bridges a short startup gap. If no valid SoC becomes available, active control is stopped.
- Discharge follows eligible household load and is capped to avoid battery export.
- Disabling control retries the safe zero command until the control entities are available, then stops writing so manual battery control remains possible.
- PV surplus charging remains available according to the selected policy.

The current actual-savings metric is intentionally a gross battery metric: measured charge energy is split into PV and grid energy, while every measured discharge-counter delta is credited at the applicable import price. Battery export and EV consumption are not yet removed from the discharge credit.

The integration also maintains `battery_strategy_features.json.gz`: a compact,
atomic 15-minute observation store with 180-day retention. During the Phase-3
observation window it is diagnostic only and cannot affect production
forecasts, plans or battery commands. Store health and growth are included in
integration diagnostics; raw feature records are intentionally not exposed as
HA entities.

## Troubleshooting

Before opening an issue, enable debug tracing temporarily and include Home Assistant diagnostics, the integration version, battery model, price source and relevant entity units. Remove addresses, serial numbers and other private data.

## Development

```bash
python3 -m py_compile custom_components/battery_strategy/*.py
python3 -m pytest -q tests
```

The repository is validated with HACS Action and Hassfest. Release tags must match the version in `manifest.json`.

## Compatibility and support

- Home Assistant: tested with `2026.7.x`
- Price source: Tibber Prices with 15-minute chart data
- Active control: Zendure-compatible AC mode and input/output limit entities
- Generic batteries: monitoring only unless a supported actuator profile is implemented

This is beta software controlling real hardware. Keep independent BMS protections active and verify behavior after every Home Assistant or integration update.

## License

MIT

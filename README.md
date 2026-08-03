# Battery Strategy

Battery Strategy plans and controls a Home Assistant battery using quarter-hour electricity prices, learned house load, PV forecasts and live grid measurements. EV charging can be excluded from automatic battery discharge.

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

## Configuration

- **Mode:** enable control, PV charging, grid charging and discharge policy
- **EV policy:** exclude EV charging and optionally block all battery discharge while EV charging
- **Battery and prices:** usable SoC, capacity, power, RTE, margin, feed-in tariff and PV capacity
- **Manual override:** manual charge or discharge; the integration services optionally accept a duration

Existing installations preserve their stored control state. A fresh installation starts disabled.

## Safety behavior

- Stale or unavailable grid inputs zero both battery limits once and remain in fail-safe until measurements recover.
- A persisted SoC bridges a short startup gap. If no valid SoC becomes available, active control is stopped.
- Discharge follows eligible household load and is capped to avoid battery export.
- Disabling control writes zero limits once, then stops writing so manual battery control remains possible.
- PV surplus charging remains available according to the selected policy.

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

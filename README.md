# Homeserver Energy Strategy

Home Assistant package, dashboard, scripts and tests for the local battery/PV strategy.

## Main files

- `custom_components/battery_strategy/`: Home Assistant custom integration for planning, live control, forecasting and diagnostics.
- `codex_energy_strategy.yaml`: neutral Home Assistant support package for live grid/battery/PV helper signals and recorder retention.
- `lovelace.dashboard_battery_strategy_parallel`: Battery Strategy dashboard export.
- `tests/test_hacs_strategy.py`: regression tests for strategy, optimizer and actuation behavior.

## Secrets

Secrets are intentionally not committed. The HA recorder URL is referenced as:

```yaml
recorder:
  db_url: !secret recorder_db_url
```

Add the real value on the HA host in `/config/secrets.yaml`. See `secrets.example.yaml` for the expected key.

## Basic checks

```bash
python3 -m py_compile custom_components/battery_strategy/*.py scripts/*.py
python3 -m pytest tests
python3 -m json.tool lovelace.dashboard_battery_strategy_parallel >/dev/null
```

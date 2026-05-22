# Homeserver Energy Strategy

Home Assistant package, dashboard, scripts and tests for the local battery/PV strategy.

## Main files

- `codex_energy_strategy.yaml`: Home Assistant package for sensors, automations and battery actuation.
- `lovelace.dashboard_speicherstrategie`: Home Assistant dashboard storage export.
- `scripts/battery_strategy_dryrun.py`: optimizer, forecast, backtest and actual savings logic.
- `tests/test_battery_strategy.py`: regression tests for optimizer behavior.
- `scripts/ha-backup-webdav-sync.*`: HA backup export/sync helper units and script.

## Secrets

Secrets are intentionally not committed. The HA recorder URL is referenced as:

```yaml
recorder:
  db_url: !secret recorder_db_url
```

Add the real value on the HA host in `/config/secrets.yaml`. See `secrets.example.yaml` for the expected key.

## Basic checks

```bash
python3 -m py_compile scripts/battery_strategy_dryrun.py
python3 -m pytest tests
python3 -m json.tool lovelace.dashboard_speicherstrategie >/dev/null
```

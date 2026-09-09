import argparse
import datetime as dt
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.contracts import (
    DataQuality,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadFeatureValue,
    QualityFlag,
    SlotKey,
)
from custom_components.battery_strategy.feature_store import CompressedFeatureStore
from scripts.battery_strategy_dhw_walkforward import evaluate, first_cycle_energy

SLOT_MS = 15 * 60 * 1000
TZ = ZoneInfo("Europe/Berlin")


def _slot(
    local: dt.datetime,
    energy_kwh: float,
    temperature_c: float,
    charging_fraction: float,
    quality: DataQuality = DataQuality(),
) -> HistoricalFeatureSlot:
    start_ms = round(local.astimezone(dt.UTC).timestamp() * 1000)
    return HistoricalFeatureSlot(
        SlotKey(start_ms, start_ms + SLOT_MS),
        energy_kwh,
        0.0,
        energy_kwh,
        0.0,
        0.0,
        0.0,
        0.0,
        30.0,
        load_components=(
            LoadComponentEnergy(
                "heat_pump_dhw",
                energy_kwh,
                quality=quality,
                features=(
                    LoadFeatureValue("dhw_temperature_c", temperature_c),
                    LoadFeatureValue("dhw_target_c", 53.0),
                    LoadFeatureValue("dhw_differential_c", 9.0),
                    LoadFeatureValue("dhw_charging_fraction", charging_fraction),
                    LoadFeatureValue("outdoor_temperature_c", 10.0),
                    LoadFeatureValue("circulation_fraction", 0.0),
                ),
            ),
        ),
    )


def _cycle(day: int) -> tuple[HistoricalFeatureSlot, ...]:
    start = dt.datetime(2026, 8, day, 3, 0, tzinfo=TZ)
    return (
        _slot(start - dt.timedelta(minutes=15), 0.0, 44.2, 0.0),
        _slot(start, 0.60, 44.0, 1.0),
        _slot(start + dt.timedelta(minutes=15), 0.75, 48.0, 1.0),
        _slot(start + dt.timedelta(minutes=30), 0.30, 54.0, 0.4),
        _slot(start + dt.timedelta(minutes=45), 0.0, 54.0, 0.0),
    )


def test_first_cycle_energy_does_not_include_later_cycle() -> None:
    forecast = (0.0, 0.30, 0.20, 0.005, 0.45, 0.35)

    assert first_cycle_energy(forecast) == 0.50


def test_walkforward_reuses_production_cycles_and_matches_one_event(tmp_path) -> None:
    path = tmp_path / "features.json.gz"
    store = CompressedFeatureStore(path)
    store.initialize()
    evaluation_start = dt.datetime(2026, 8, 9, 3, 0, tzinfo=TZ)
    context_slot = _slot(evaluation_start - dt.timedelta(minutes=75), 0.0, 44.5, 0.0)
    unusable_future = _slot(
        evaluation_start + dt.timedelta(hours=1),
        5.0,
        54.0,
        0.0,
        DataQuality(1.0, (QualityFlag.ESTIMATED,)),
    )
    store.upsert((*_cycle(2), context_slot, *_cycle(9), unusable_future))

    result = evaluate(
        argparse.Namespace(
            feature_store=path,
            label="test",
            timezone="Europe/Berlin",
            target_c=53.0,
            hysteresis_k=9.0,
            allowed_windows="00:00-05:00",
            lead_hours=[1],
            horizon_hours=8,
            minimum_history_days=0,
        )
    )

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["actual_start_ms"] == round(
        evaluation_start.astimezone(dt.UTC).timestamp() * 1000
    )
    assert row["actual_energy_kwh"] == 1.65
    assert row["predicted_start_ms"] is not None
    assert row["slot_mae_kwh"] < 0.1
    assert result["summary"]["1"]["missed_cycles"] == 0

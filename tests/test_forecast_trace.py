import datetime as dt
import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from custom_components.battery_strategy import forecast_trace as trace_module
from custom_components.battery_strategy.contracts import (
    DataQuality,
    ForecastBundle,
    ForecastSlot,
    LoadForecast,
    LoadForecastComponent,
    PvForecast,
    QualityFlag,
    QuantileEnergy,
    SlotKey,
)
from custom_components.battery_strategy.forecast_trace import (
    FORECAST_TRACE_RETENTION_DAYS,
    SLOT_MS,
    append_forecast_trace,
)


def forecast_bundle(
    generated_at_ms: int, *, slot_count: int = 2, component_count: int = 1
) -> ForecastBundle:
    start_ms = (generated_at_ms // SLOT_MS + 1) * SLOT_MS
    slots = tuple(
        SlotKey(start_ms + index * SLOT_MS, start_ms + (index + 1) * SLOT_MS)
        for index in range(slot_count)
    )
    load_slots = tuple(
        ForecastSlot(slot, QuantileEnergy(0.1, 0.08, 0.12, 20)) for slot in slots
    )
    pv_slots = tuple(
        ForecastSlot(slot, QuantileEnergy(0.2), DataQuality(1.0)) for slot in slots
    )
    return ForecastBundle(
        load=LoadForecast(
            "load-id",
            generated_at_ms,
            generated_at_ms - SLOT_MS,
            "load-v2",
            load_slots,
            tuple(
                LoadForecastComponent(
                    "general_house" if component_count == 1 else f"component_{index}",
                    "general-v1",
                    generated_at_ms - SLOT_MS,
                    tuple(
                        ForecastSlot(
                            slot,
                            QuantileEnergy(
                                0.1 / component_count,
                                0.08 / component_count,
                                0.12 / component_count,
                                20,
                            ),
                            DataQuality(0.9, (QualityFlag.MISSING_WEATHER,)),
                        )
                        for slot in slots
                    ),
                )
                for index in range(component_count)
            ),
        ),
        pv=PvForecast(
            "pv-id",
            generated_at_ms,
            generated_at_ms - SLOT_MS,
            "pv-v2",
            pv_slots,
        ),
    )


def test_trace_preserves_contract_metadata_quantiles_and_components(tmp_path):
    generated_at_ms = int(
        dt.datetime(2027, 1, 20, 10, 7, tzinfo=dt.UTC).timestamp() * 1000
    )

    path = append_forecast_trace(tmp_path, forecast_bundle(generated_at_ms))

    assert path is not None
    payload = json.loads(gzip.decompress(path.read_bytes()))
    assert payload["non_authoritative"] is True
    assert payload["generated_at_ms"] == generated_at_ms
    assert payload["load"]["model_version"] == "load-v2"
    assert payload["load"]["slots"][0][2:6] == [0.1, 0.08, 0.12, 20]
    assert payload["load"]["components"][0]["component_key"] == "general_house"
    assert payload["load"]["components"][0]["slots"][0][7] == ["missing_weather"]
    assert payload["pv"]["model_version"] == "pv-v2"


def test_trace_writes_only_one_vintage_per_quarter(tmp_path):
    first_ms = int(dt.datetime(2027, 1, 20, 10, 2, tzinfo=dt.UTC).timestamp() * 1000)
    second_ms = first_ms + 5 * 60 * 1000

    first = append_forecast_trace(tmp_path, forecast_bundle(first_ms))
    second = append_forecast_trace(tmp_path, forecast_bundle(second_ms))

    assert first is not None
    assert second is None
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1


def test_trace_retention_removes_only_expired_date_directories(tmp_path):
    generated = dt.datetime(2027, 2, 1, 10, 2, tzinfo=dt.UTC)
    expired = generated.date() - dt.timedelta(days=FORECAST_TRACE_RETENTION_DAYS)
    retained = generated.date() - dt.timedelta(days=FORECAST_TRACE_RETENTION_DAYS - 1)
    (tmp_path / expired.isoformat()).mkdir()
    (tmp_path / retained.isoformat()).mkdir()
    (tmp_path / "operator-notes").mkdir()

    append_forecast_trace(tmp_path, forecast_bundle(int(generated.timestamp() * 1000)))

    assert not (tmp_path / expired.isoformat()).exists()
    assert (tmp_path / retained.isoformat()).exists()
    assert (tmp_path / "operator-notes").exists()
    assert not list(Path(tmp_path).rglob("*.tmp"))


def test_trace_caps_slots_and_components(tmp_path):
    generated_at_ms = int(
        dt.datetime(2027, 1, 20, 10, 7, tzinfo=dt.UTC).timestamp() * 1000
    )
    bundle = forecast_bundle(generated_at_ms, slot_count=193, component_count=33)

    path = append_forecast_trace(tmp_path, bundle)

    payload = json.loads(gzip.decompress(path.read_bytes()))
    assert payload["truncated"] is True
    assert len(payload["load"]["slots"]) == 192
    assert len(payload["pv"]["slots"]) == 192
    assert len(payload["load"]["components"]) == 32


def test_trace_cleans_temporary_file_after_serialization_failure(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise TypeError("not serializable")

    monkeypatch.setattr(trace_module.json, "dump", fail)

    with pytest.raises(TypeError, match="not serializable"):
        append_forecast_trace(tmp_path, forecast_bundle(1_800_000_000_000))

    assert not list(tmp_path.rglob("*.tmp"))


def test_trace_preserves_independent_load_and_pv_generation_times(tmp_path):
    bundle = forecast_bundle(1_800_000_000_000)
    bundle = replace(
        bundle,
        pv=replace(bundle.pv, generated_at_ms=bundle.pv.generated_at_ms + 60_000),
    )

    path = append_forecast_trace(tmp_path, bundle)

    payload = json.loads(gzip.decompress(path.read_bytes()))
    assert payload["load"]["generated_at_ms"] == 1_800_000_000_000
    assert payload["pv"]["generated_at_ms"] == 1_800_000_060_000


def test_concurrent_writers_publish_one_complete_first_vintage(tmp_path):
    bundle = forecast_bundle(1_800_000_000_000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _index: append_forecast_trace(tmp_path, bundle), range(8))
        )

    assert sum(path is not None for path in results) == 1
    files = list(tmp_path.rglob("*.json.gz"))
    assert len(files) == 1
    payload = json.loads(gzip.decompress(files[0].read_bytes()))
    assert payload["schema_version"] == 1
    assert payload["load"]["forecast_id"] == "load-id"
    assert not list(tmp_path.rglob("*.tmp"))


def test_trace_payload_has_no_installation_mapping_fields(tmp_path):
    path = append_forecast_trace(tmp_path, forecast_bundle(1_800_000_000_000))
    rendered = gzip.decompress(path.read_bytes()).decode()

    for forbidden in (
        "entity_id",
        "hostname",
        "device_id",
        "serial_number",
        "mqtt_topic",
        "config_path",
    ):
        assert forbidden not in rendered

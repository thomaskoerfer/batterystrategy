"""Recorder-independent shadow feature aggregation and persistence."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import (
    DataQuality,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadFeatureValue,
    QualityFlag,
    SlotKey,
)
from .contracts.common import CONTRACT_SCHEMA_VERSION, SLOT_MS

FEATURE_STORE_RETENTION_DAYS = 180
FEATURE_STORE_BACKUP_SUFFIX = ".schema{version}.bak"
MAX_CONTINUOUS_SAMPLE_GAP_MS = 120_000


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    """One normalized live observation used only for feature aggregation."""

    timestamp_ms: int
    grid_import_w: float
    grid_export_w: float
    pv_generation_w: float
    battery_power_w: float
    ev_charge_w: float
    price_ct_per_kwh: float | None
    quality_flags: tuple[QualityFlag, ...] = ()
    load_components_w: tuple[tuple[str, float], ...] = ()
    load_component_features: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()


@dataclass(slots=True)
class _FeatureAccumulator:
    weighted_value_ms: float = 0.0
    covered_ms: int = 0


@dataclass(slots=True)
class _Accumulator:
    start_ms: int
    covered_ms: int = 0
    house_load_no_ev_kwh: float = 0.0
    pv_generation_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0
    ev_charge_kwh: float = 0.0
    price_weighted_ct_ms: float = 0.0
    price_covered_ms: int = 0
    flags: set[QualityFlag] = field(default_factory=set)
    load_components_kwh: dict[str, float] = field(default_factory=dict)
    load_component_features: dict[str, dict[str, _FeatureAccumulator]] = field(
        default_factory=dict
    )


class FeatureAggregator:
    """Aggregate irregular power observations into canonical UTC slots."""

    def __init__(self) -> None:
        self._previous: FeatureObservation | None = None
        self._active: _Accumulator | None = None

    def observe(
        self, observation: FeatureObservation
    ) -> tuple[HistoricalFeatureSlot, ...]:
        """Consume one observation and return newly finalized slots."""
        if observation.timestamp_ms < 0:
            raise ValueError("observation timestamp must be non-negative")
        if (
            self._previous is None
            or observation.timestamp_ms <= self._previous.timestamp_ms
        ):
            self._previous = observation
            self._active = _Accumulator(_slot_start(observation.timestamp_ms))
            if observation.timestamp_ms > self._active.start_ms:
                self._active.flags.add(QualityFlag.RESTART_GAP)
            return ()

        previous = self._previous
        self._previous = observation
        if self._active is None:
            self._active = _Accumulator(_slot_start(previous.timestamp_ms))
            self._active.flags.add(QualityFlag.RESTART_GAP)

        if (
            observation.timestamp_ms - previous.timestamp_ms
            > MAX_CONTINUOUS_SAMPLE_GAP_MS
        ):
            return self._advance(previous.timestamp_ms, observation.timestamp_ms, None)
        return self._advance(previous.timestamp_ms, observation.timestamp_ms, previous)

    def _advance(
        self,
        start_ms: int,
        end_ms: int,
        sample: FeatureObservation | None,
    ) -> tuple[HistoricalFeatureSlot, ...]:
        finalized: list[HistoricalFeatureSlot] = []
        cursor = start_ms
        while cursor < end_ms:
            assert self._active is not None
            expected_start = _slot_start(cursor)
            if self._active.start_ms != expected_start:
                self._active = _Accumulator(expected_start)
                self._active.flags.add(QualityFlag.RESTART_GAP)
            boundary = self._active.start_ms + SLOT_MS
            segment_end = min(end_ms, boundary)
            duration_ms = segment_end - cursor
            if sample is None:
                self._active.flags.add(QualityFlag.RESTART_GAP)
            else:
                self._integrate(sample, duration_ms)
            cursor = segment_end
            if cursor == boundary:
                finalized.append(self._finalize())
                self._active = _Accumulator(boundary)
                if sample is None and cursor < end_ms:
                    self._active.flags.add(QualityFlag.RESTART_GAP)
        return tuple(finalized)

    def _integrate(self, sample: FeatureObservation, duration_ms: int) -> None:
        assert self._active is not None
        hours = duration_ms / 3_600_000.0
        grid_import_w = max(0.0, float(sample.grid_import_w))
        grid_export_w = max(0.0, float(sample.grid_export_w))
        pv_w = max(0.0, float(sample.pv_generation_w))
        battery_w = float(sample.battery_power_w)
        ev_w = max(0.0, float(sample.ev_charge_w))
        house_total_w = max(0.0, grid_import_w + pv_w + battery_w - grid_export_w)
        house_no_ev_w = max(0.0, house_total_w - ev_w)

        self._active.covered_ms += duration_ms
        self._active.house_load_no_ev_kwh += house_no_ev_w * hours / 1000.0
        self._active.pv_generation_kwh += pv_w * hours / 1000.0
        self._active.grid_import_kwh += grid_import_w * hours / 1000.0
        self._active.grid_export_kwh += grid_export_w * hours / 1000.0
        self._active.battery_charge_kwh += max(0.0, -battery_w) * hours / 1000.0
        self._active.battery_discharge_kwh += max(0.0, battery_w) * hours / 1000.0
        self._active.ev_charge_kwh += ev_w * hours / 1000.0
        self._active.flags.update(sample.quality_flags)
        for component_key, power_w in sample.load_components_w:
            self._active.load_components_kwh[component_key] = (
                self._active.load_components_kwh.get(component_key, 0.0)
                + max(0.0, float(power_w)) * hours / 1000.0
            )
        for component_key, features in sample.load_component_features:
            component = self._active.load_component_features.setdefault(
                component_key, {}
            )
            for feature_key, value in features:
                feature = component.setdefault(feature_key, _FeatureAccumulator())
                feature.weighted_value_ms += float(value) * duration_ms
                feature.covered_ms += duration_ms
        if sample.price_ct_per_kwh is None:
            self._active.flags.add(QualityFlag.MISSING_PRICE)
        else:
            self._active.price_weighted_ct_ms += (
                float(sample.price_ct_per_kwh) * duration_ms
            )
            self._active.price_covered_ms += duration_ms

    def _finalize(self) -> HistoricalFeatureSlot:
        assert self._active is not None
        coverage = min(1.0, self._active.covered_ms / SLOT_MS)
        flags = set(self._active.flags)
        if coverage < 0.999:
            flags.add(QualityFlag.ESTIMATED)
        component_flags = set()
        if coverage < 0.999:
            component_flags.add(QualityFlag.ESTIMATED)
        if sum(self._active.load_components_kwh.values()) > (
            self._active.house_load_no_ev_kwh + 1e-9
        ):
            component_flags.add(QualityFlag.COMPONENT_MISMATCH)
        price = (
            self._active.price_weighted_ct_ms / self._active.price_covered_ms
            if self._active.price_covered_ms
            else None
        )
        return HistoricalFeatureSlot(
            slot=SlotKey(self._active.start_ms, self._active.start_ms + SLOT_MS),
            house_load_no_ev_kwh=self._active.house_load_no_ev_kwh,
            pv_generation_kwh=self._active.pv_generation_kwh,
            grid_import_kwh=self._active.grid_import_kwh,
            grid_export_kwh=self._active.grid_export_kwh,
            battery_charge_kwh=self._active.battery_charge_kwh,
            battery_discharge_kwh=self._active.battery_discharge_kwh,
            ev_charge_kwh=self._active.ev_charge_kwh,
            price_ct_per_kwh=price,
            quality=DataQuality(coverage, tuple(sorted(flags, key=str))),
            load_components=tuple(
                LoadComponentEnergy(
                    key,
                    value,
                    DataQuality(coverage, tuple(sorted(component_flags, key=str))),
                    tuple(
                        LoadFeatureValue(
                            feature_key,
                            feature.weighted_value_ms / feature.covered_ms,
                            DataQuality(feature.covered_ms / SLOT_MS),
                        )
                        for feature_key, feature in sorted(
                            self._active.load_component_features.get(key, {}).items()
                        )
                        if feature.covered_ms
                    ),
                )
                for key, value in sorted(
                    {
                        **{key: 0.0 for key in self._active.load_component_features},
                        **self._active.load_components_kwh,
                    }.items()
                )
            ),
        )

    @property
    def active_coverage(self) -> float:
        """Return current incomplete-slot coverage for diagnostics."""
        if self._active is None:
            return 0.0
        return min(1.0, self._active.covered_ms / SLOT_MS)


class CompressedFeatureStore:
    """Atomic compressed persistence for finalized feature slots."""

    def __init__(
        self, path: str | Path, retention_days: int = FEATURE_STORE_RETENTION_DAYS
    ):
        self.path = Path(path)
        self.retention_days = max(1, int(retention_days))
        self._slots: dict[int, HistoricalFeatureSlot] = {}
        self.last_error: str | None = None

    def initialize(self) -> None:
        """Load and validate an existing store without failing integration setup."""
        try:
            slots, schema_version = self._read()
            self._slots = {slot.slot.start_ms: slot for slot in slots}
            if schema_version < CONTRACT_SCHEMA_VERSION:
                self._migrate_to_current(schema_version)
            self.last_error = None
        except (
            OSError,
            EOFError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as err:
            self._slots = {}
            self.last_error = f"{type(err).__name__}: {err}"

    def load(self, start_ms: int, end_ms: int) -> tuple[HistoricalFeatureSlot, ...]:
        """Return sorted finalized slots in the half-open requested range."""
        return tuple(
            self._slots[key] for key in sorted(self._slots) if start_ms <= key < end_ms
        )

    def upsert(self, slots: tuple[HistoricalFeatureSlot, ...]) -> None:
        """Upsert slots, enforce retention, and atomically persist the envelope."""
        if not slots:
            return
        for slot in slots:
            self._slots[slot.slot.start_ms] = slot
        newest_end_ms = max(item.slot.end_ms for item in self._slots.values())
        cutoff_ms = newest_end_ms - self.retention_days * 86_400_000
        self._slots = {
            key: value
            for key, value in self._slots.items()
            if value.slot.end_ms > cutoff_ms
        }
        envelope = {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "retention_days": self.retention_days,
            "slots": [_serialize(self._slots[key]) for key in sorted(self._slots)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        with gzip.open(tmp, "wb", compresslevel=6) as handle:
            handle.write(payload)
        os.replace(tmp, self.path)
        self.last_error = None

    def diagnostics(self, active_coverage: float = 0.0) -> dict[str, object]:
        """Return bounded operational diagnostics without feature payloads."""
        ordered = [self._slots[key] for key in sorted(self._slots)]
        last = ordered[-1] if ordered else None
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "retention_days": self.retention_days,
            "slot_count": len(ordered),
            "oldest_slot_start_ms": ordered[0].slot.start_ms if ordered else None,
            "newest_slot_start_ms": last.slot.start_ms if last else None,
            "last_slot_coverage": last.quality.coverage if last else None,
            "last_slot_flags": [flag.value for flag in last.quality.flags]
            if last
            else [],
            "last_component_keys": [
                component.component_key for component in last.load_components
            ]
            if last
            else [],
            "last_component_feature_keys": {
                component.component_key: [
                    feature.feature_key for feature in component.features
                ]
                for component in last.load_components
            }
            if last
            else {},
            "active_slot_coverage": round(float(active_coverage), 4),
            "file_size_bytes": self._file_size(),
            "last_error": self.last_error,
            "authoritative": False,
        }

    def _read(self) -> tuple[tuple[HistoricalFeatureSlot, ...], int]:
        if not self.path.exists():
            return (), CONTRACT_SCHEMA_VERSION
        raw = gzip.decompress(self.path.read_bytes())
        envelope = json.loads(raw.decode("utf-8"))
        schema_version = int(envelope.get("schema_version", 0))
        if schema_version not in (1, 2, CONTRACT_SCHEMA_VERSION):
            raise ValueError("unsupported feature store schema")
        return (
            tuple(_deserialize(item) for item in envelope.get("slots", [])),
            schema_version,
        )

    def _migrate_to_current(self, schema_version: int) -> None:
        """Atomically migrate the complete store while preserving rollback data."""
        backup = self.path.with_name(
            self.path.name + FEATURE_STORE_BACKUP_SUFFIX.format(version=schema_version)
        )
        # The fixed backup name always represents the exact input to this
        # migration, including after an operator has restored an older schema.
        shutil.copy2(self.path, backup)
        self._write_envelope(CONTRACT_SCHEMA_VERSION)

    def downgrade_to_schema_one(self, destination: str | Path | None = None) -> Path:
        """Write a complete schema-1 store, dropping only component metadata."""
        target = Path(destination) if destination is not None else self.path
        self._write_envelope(1, target=target)
        return target

    def downgrade_to_schema_two(self, destination: str | Path | None = None) -> Path:
        """Write schema 2, retaining component energy but dropping features."""
        target = Path(destination) if destination is not None else self.path
        self._write_envelope(2, target=target)
        return target

    def _write_envelope(
        self, schema_version: int, *, target: Path | None = None
    ) -> None:
        target = target or self.path
        envelope = {
            "schema_version": int(schema_version),
            "retention_days": self.retention_days,
            "slots": [
                _serialize(
                    self._slots[key],
                    include_components=schema_version >= 2,
                    include_component_features=schema_version >= 3,
                )
                for key in sorted(self._slots)
            ],
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with gzip.open(tmp, "wb", compresslevel=6) as handle:
            handle.write(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))
        os.replace(tmp, target)

    def _file_size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


class ExecutorFeatureStore:
    """Async contract adapter keeping compressed file I/O off the event loop."""

    def __init__(
        self,
        backend: CompressedFeatureStore,
        run_in_executor: Callable[..., Awaitable],
    ) -> None:
        self._backend = backend
        self._run_in_executor = run_in_executor

    async def load(
        self, start_ms: int, end_ms: int
    ) -> tuple[HistoricalFeatureSlot, ...]:
        return await self._run_in_executor(self._backend.load, start_ms, end_ms)

    async def upsert(self, slots: tuple[HistoricalFeatureSlot, ...]) -> None:
        await self._run_in_executor(self._backend.upsert, slots)

    def diagnostics(self, active_coverage: float = 0.0) -> dict[str, object]:
        return self._backend.diagnostics(active_coverage)

    @property
    def last_error(self) -> str | None:
        return self._backend.last_error

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        self._backend.last_error = value


def _slot_start(timestamp_ms: int) -> int:
    return int(timestamp_ms) // SLOT_MS * SLOT_MS


def _serialize(
    slot: HistoricalFeatureSlot,
    *,
    include_components: bool = True,
    include_component_features: bool = True,
) -> dict[str, object]:
    result = {
        "start_ms": slot.slot.start_ms,
        "house_load_no_ev_kwh": slot.house_load_no_ev_kwh,
        "pv_generation_kwh": slot.pv_generation_kwh,
        "grid_import_kwh": slot.grid_import_kwh,
        "grid_export_kwh": slot.grid_export_kwh,
        "battery_charge_kwh": slot.battery_charge_kwh,
        "battery_discharge_kwh": slot.battery_discharge_kwh,
        "ev_charge_kwh": slot.ev_charge_kwh,
        "price_ct_per_kwh": slot.price_ct_per_kwh,
        "coverage": slot.quality.coverage,
        "flags": [flag.value for flag in slot.quality.flags],
    }
    if include_components:
        result["load_components"] = []
        for item in slot.load_components:
            component = {
                "key": item.component_key,
                "energy_kwh": item.energy_kwh,
                "coverage": item.quality.coverage,
                "flags": [flag.value for flag in item.quality.flags],
            }
            if include_component_features:
                component["features"] = [
                    {
                        "key": feature.feature_key,
                        "value": feature.value,
                        "coverage": feature.quality.coverage,
                        "flags": [flag.value for flag in feature.quality.flags],
                    }
                    for feature in item.features
                ]
            result["load_components"].append(component)
    return result


def _deserialize(item: dict) -> HistoricalFeatureSlot:
    start_ms = int(item["start_ms"])
    return HistoricalFeatureSlot(
        slot=SlotKey(start_ms, start_ms + SLOT_MS),
        house_load_no_ev_kwh=float(item["house_load_no_ev_kwh"]),
        pv_generation_kwh=float(item["pv_generation_kwh"]),
        grid_import_kwh=float(item["grid_import_kwh"]),
        grid_export_kwh=float(item["grid_export_kwh"]),
        battery_charge_kwh=float(item["battery_charge_kwh"]),
        battery_discharge_kwh=float(item["battery_discharge_kwh"]),
        ev_charge_kwh=float(item["ev_charge_kwh"]),
        price_ct_per_kwh=(
            None
            if item.get("price_ct_per_kwh") is None
            else float(item["price_ct_per_kwh"])
        ),
        quality=DataQuality(
            float(item.get("coverage", 0.0)),
            tuple(QualityFlag(value) for value in item.get("flags", [])),
        ),
        load_components=tuple(
            LoadComponentEnergy(
                str(item["key"]),
                float(item["energy_kwh"]),
                DataQuality(
                    float(item.get("coverage", 0.0)),
                    tuple(QualityFlag(value) for value in item.get("flags", [])),
                ),
                tuple(
                    LoadFeatureValue(
                        str(feature["key"]),
                        float(feature["value"]),
                        DataQuality(
                            float(feature.get("coverage", 0.0)),
                            tuple(
                                QualityFlag(value) for value in feature.get("flags", [])
                            ),
                        ),
                    )
                    for feature in item.get("features", [])
                ),
            )
            for item in item.get("load_components", [])
        ),
    )

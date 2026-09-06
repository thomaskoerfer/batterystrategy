#!/usr/bin/env python3
"""Evaluate stored forecast vintages against finalized feature-store actuals."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SLOT_MS = 15 * 60 * 1000
TRACE_SCHEMA_VERSION = 1
SUPPORTED_FEATURE_STORE_SCHEMAS = frozenset({1, 2, 3})
DEFAULT_TRACE_DIRECTORY = "/config/battery_strategy_forecast_trace"
DEFAULT_FEATURE_STORE = "/config/battery_strategy_features.json.gz"
LOAD_INVALID_FLAGS = frozenset(
    {
        "estimated",
        "missing_grid",
        "missing_pv",
        "missing_battery",
        "missing_ev",
        "counter_reset",
        "restart_gap",
    }
)
PV_INVALID_FLAGS = frozenset(
    {"estimated", "missing_pv", "counter_reset", "restart_gap"}
)


@dataclass(frozen=True, slots=True)
class ForecastObservation:
    """One P50 forecast value at a known vintage and target slot."""

    series: str
    model_version: str
    generated_at_ms: int
    start_ms: int
    end_ms: int
    forecast_kwh: float
    p10_kwh: float | None
    p90_kwh: float | None
    calibration_samples: int
    forecast_coverage: float
    forecast_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Residual:
    """One matured forecast-to-actual comparison."""

    series: str
    model_version: str
    lead_bucket: str
    generated_at_ms: int
    start_ms: int
    forecast_kwh: float
    actual_kwh: float
    p10_kwh: float | None
    p90_kwh: float | None
    forecast_coverage: float

    @property
    def error_kwh(self) -> float:
        return self.forecast_kwh - self.actual_kwh


def load_forecast_observations(
    trace_directory: str | Path,
    *,
    start_generated_ms: int = 0,
    end_generated_ms: int = 2**63 - 1,
) -> list[ForecastObservation]:
    """Load valid forecast traces from strict date directories."""
    root = Path(trace_directory)
    if not root.exists():
        return []
    observations: list[ForecastObservation] = []
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir() or not _is_iso_date(day_dir.name):
            continue
        for path in sorted(day_dir.glob("*.json.gz")):
            try:
                payload = json.loads(gzip.decompress(path.read_bytes()))
                if int(payload.get("schema_version", -1)) != TRACE_SCHEMA_VERSION:
                    continue
                vintage_generated_at_ms = int(payload["generated_at_ms"])
                if not (
                    start_generated_ms <= vintage_generated_at_ms <= end_generated_ms
                ):
                    continue
                if payload.get("non_authoritative") is not True:
                    continue
                load_generated_at_ms = int(
                    payload["load"].get("generated_at_ms", vintage_generated_at_ms)
                )
                pv_generated_at_ms = int(
                    payload["pv"].get("generated_at_ms", vintage_generated_at_ms)
                )
                observations.extend(
                    _series_observations(
                        "load_total", payload["load"], load_generated_at_ms
                    )
                )
                observations.extend(
                    _series_observations("pv", payload["pv"], pv_generated_at_ms)
                )
                for component in payload["load"].get("components", []):
                    key = str(component.get("component_key") or "")
                    if key:
                        observations.extend(
                            _series_observations(
                                f"load_component:{key}",
                                component,
                                load_generated_at_ms,
                            )
                        )
            except OSError, EOFError, UnicodeError, ValueError, TypeError, KeyError:
                continue
    return observations


def load_actuals(feature_store: str | Path) -> dict[int, dict]:
    """Load the compact feature-store envelope without depending on HA."""
    payload = json.loads(gzip.decompress(Path(feature_store).read_bytes()))
    schema_version = int(payload.get("schema_version", -1))
    if schema_version not in SUPPORTED_FEATURE_STORE_SCHEMAS:
        raise ValueError(f"unsupported feature-store schema: {schema_version}")
    return {
        int(item["start_ms"]): item
        for item in payload.get("slots", [])
        if isinstance(item, dict) and "start_ms" in item
    }


def compare_forecasts(
    observations: list[ForecastObservation],
    actuals: dict[int, dict],
    *,
    as_of_ms: int,
) -> list[Residual]:
    """Match only complete future target slots with usable finalized actuals."""
    residuals: list[Residual] = []
    for observation in observations:
        # A forecast created inside its target slot is not comparable with the
        # final whole-slot actual. Current and unfinished slots are excluded.
        if observation.start_ms < observation.generated_at_ms:
            continue
        if observation.end_ms > as_of_ms:
            continue
        actual = actuals.get(observation.start_ms)
        actual_kwh = _actual_value(observation.series, actual)
        if actual_kwh is None:
            continue
        residuals.append(
            Residual(
                series=observation.series,
                model_version=observation.model_version,
                lead_bucket=_lead_bucket(
                    observation.start_ms - observation.generated_at_ms
                ),
                generated_at_ms=observation.generated_at_ms,
                start_ms=observation.start_ms,
                forecast_kwh=observation.forecast_kwh,
                actual_kwh=actual_kwh,
                p10_kwh=observation.p10_kwh,
                p90_kwh=observation.p90_kwh,
                forecast_coverage=observation.forecast_coverage,
            )
        )
    return residuals


def summarize(residuals: list[Residual]) -> list[dict[str, object]]:
    """Aggregate deterministic error metrics by model, series and lead time."""
    grouped: dict[tuple[str, str, str], list[Residual]] = defaultdict(list)
    for item in residuals:
        grouped[(item.series, item.model_version, item.lead_bucket)].append(item)
    rows: list[dict[str, object]] = []
    for (series, model_version, lead_bucket), items in sorted(grouped.items()):
        errors = [item.error_kwh for item in items]
        intervals = [
            item
            for item in items
            if item.p10_kwh is not None and item.p90_kwh is not None
        ]
        actual_kwh = sum(item.actual_kwh for item in items)
        forecast_kwh = sum(item.forecast_kwh for item in items)
        rows.append(
            {
                "series": series,
                "model_version": model_version,
                "lead_bucket": lead_bucket,
                "samples": len(items),
                "mae_kwh": sum(abs(value) for value in errors) / len(items),
                "bias_kwh": sum(errors) / len(items),
                "rmse_kwh": math.sqrt(
                    sum(value * value for value in errors) / len(items)
                ),
                "wape_pct": (
                    100.0 * sum(abs(value) for value in errors) / actual_kwh
                    if actual_kwh > 1e-9
                    else None
                ),
                "actual_kwh": actual_kwh,
                "forecast_kwh": forecast_kwh,
                "forecast_quality_coverage": sum(
                    item.forecast_coverage for item in items
                )
                / len(items),
                "p10_p90_samples": len(intervals),
                "p10_p90_coverage_pct": (
                    100.0
                    * sum(
                        item.p10_kwh <= item.actual_kwh <= item.p90_kwh
                        for item in intervals
                    )
                    / len(intervals)
                    if intervals
                    else None
                ),
                "p10_p90_mean_width_kwh": (
                    sum(item.p90_kwh - item.p10_kwh for item in intervals)
                    / len(intervals)
                    if intervals
                    else None
                ),
            }
        )
    return rows


def _series_observations(
    series: str, payload: dict, generated_at_ms: int
) -> list[ForecastObservation]:
    model_version = str(payload.get("model_version") or "unknown")
    observations: list[ForecastObservation] = []
    for slot in payload.get("slots", []):
        try:
            start_ms = int(slot[0])
            end_ms = int(slot[1])
            forecast_kwh = float(slot[2])
            p10_kwh = None if slot[3] is None else float(slot[3])
            p90_kwh = None if slot[4] is None else float(slot[4])
            calibration_samples = int(slot[5])
            forecast_coverage = float(slot[6])
            forecast_flags = tuple(str(value) for value in slot[7])
        except IndexError, TypeError, ValueError:
            continue
        if (
            end_ms - start_ms != SLOT_MS
            or start_ms % SLOT_MS
            or not math.isfinite(forecast_kwh)
            or forecast_kwh < 0.0
            or not 0.0 <= forecast_coverage <= 1.0
            or calibration_samples < 0
            or (p10_kwh is None) != (p90_kwh is None)
            or (
                p10_kwh is not None
                and (
                    not math.isfinite(p10_kwh)
                    or not math.isfinite(p90_kwh)
                    or not 0.0 <= p10_kwh <= forecast_kwh <= p90_kwh
                )
            )
        ):
            continue
        observations.append(
            ForecastObservation(
                series,
                model_version,
                generated_at_ms,
                start_ms,
                end_ms,
                forecast_kwh,
                p10_kwh,
                p90_kwh,
                calibration_samples,
                forecast_coverage,
                forecast_flags,
            )
        )
    return observations


def _actual_value(series: str, actual: dict | None) -> float | None:
    if actual is None or float(actual.get("coverage", 0.0)) < 0.999:
        return None
    flags = {str(value) for value in actual.get("flags", [])}
    if flags.intersection({"estimated", "counter_reset", "restart_gap"}):
        return None
    if series == "load_total":
        if flags.intersection(LOAD_INVALID_FLAGS):
            return None
        return _nonnegative_finite(actual.get("house_load_no_ev_kwh"))
    if series == "pv":
        if flags.intersection(PV_INVALID_FLAGS):
            return None
        return _nonnegative_finite(actual.get("pv_generation_kwh"))
    if not series.startswith("load_component:"):
        return None
    key = series.removeprefix("load_component:")
    for component in actual.get("load_components", []):
        if str(component.get("key")) != key:
            continue
        if float(component.get("coverage", 0.0)) < 0.999:
            return None
        if component.get("flags"):
            return None
        return _nonnegative_finite(component.get("energy_kwh"))
    return None


def _nonnegative_finite(value) -> float | None:
    try:
        result = float(value)
    except TypeError, ValueError:
        return None
    return result if math.isfinite(result) and result >= 0.0 else None


def _lead_bucket(lead_ms: int) -> str:
    if lead_ms < 60 * 60 * 1000:
        return "0-1h"
    if lead_ms < 6 * 60 * 60 * 1000:
        return "1-6h"
    if lead_ms < 24 * 60 * 60 * 1000:
        return "6-24h"
    if lead_ms < 48 * 60 * 60 * 1000:
        return "24-48h"
    return "48h+"


def _is_iso_date(value: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except ValueError:
        return False


def _write_csv(path: str | Path, rows: list[dict[str, object]]) -> None:
    fieldnames = list(rows[0]) if rows else ["series"]
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _parse_as_of(value: str | None) -> int:
    if value is None:
        return int(datetime.now(UTC).timestamp() * 1000)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include a timezone")
    return int(parsed.timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", default=DEFAULT_TRACE_DIRECTORY)
    parser.add_argument("--feature-store", default=DEFAULT_FEATURE_STORE)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--as-of")
    parser.add_argument("--json-out")
    parser.add_argument("--csv-out")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")

    as_of_ms = _parse_as_of(args.as_of)
    start_ms = as_of_ms - int(timedelta(days=args.days).total_seconds() * 1000)
    observations = load_forecast_observations(
        args.trace_dir, start_generated_ms=start_ms, end_generated_ms=as_of_ms
    )
    residuals = compare_forecasts(
        observations, load_actuals(args.feature_store), as_of_ms=as_of_ms
    )
    rows = summarize(residuals)
    report = {
        "non_authoritative": True,
        "as_of_ms": as_of_ms,
        "window_days": args.days,
        "forecast_vintages": len({item.generated_at_ms for item in observations}),
        "matured_comparisons": len(residuals),
        "metrics": rows,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    if args.csv_out:
        _write_csv(args.csv_out, rows)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Evaluate stored forecast vintages against finalized feature-store actuals."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SLOT_MS = 15 * 60 * 1000
SUPPORTED_TRACE_SCHEMAS = frozenset(range(1, 9))
SUPPORTED_FEATURE_STORE_SCHEMAS = frozenset({1, 2, 3})
DEFAULT_TRACE_DIRECTORY = "/config/battery_strategy_forecast_trace"
DEFAULT_FEATURE_STORE = "/config/battery_strategy_features.json.gz"
HEAT_PUMP_MIN_RUNS = 3
HEAT_PUMP_MIN_DHW_CYCLES = 3
HEAT_PUMP_MIN_COMPLETE_DAYS = 7
HEAT_PUMP_MIN_READY_RATE = 0.95
HEAT_PUMP_ACTIVE_KWH = 0.025
HEAT_PUMP_DHW_MAE_TOLERANCE_KWH = 0.005
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


@dataclass(frozen=True, slots=True)
class HeatPumpTraceStatus:
    """One persisted whole-heat-pump candidate outcome."""

    generated_at_ms: int
    vintage_bucket_ms: int
    status: str


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
                if (
                    int(payload.get("schema_version", -1))
                    not in SUPPORTED_TRACE_SCHEMAS
                ):
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
                heat_pump_shadow = (
                    payload.get("forecast_shadows", {}).get("heat_pump") or {}
                )
                if heat_pump_shadow.get("status") == "completed":
                    shadow_forecast = heat_pump_shadow.get("forecast") or {}
                    shadow_generated_at_ms = int(
                        shadow_forecast.get("generated_at_ms", vintage_generated_at_ms)
                    )
                    total = shadow_forecast.get("total")
                    if isinstance(total, dict):
                        observations.extend(
                            _series_observations(
                                "shadow_heat_pump_total",
                                total,
                                shadow_generated_at_ms,
                            )
                        )
                    for component in shadow_forecast.get("components", []):
                        key = str(component.get("component_key") or "")
                        if key:
                            observations.extend(
                                _series_observations(
                                    f"shadow_load_component:{key}",
                                    component,
                                    shadow_generated_at_ms,
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


def load_heat_pump_trace_statuses(
    trace_directory: str | Path,
    *,
    start_generated_ms: int = 0,
    end_generated_ms: int = 2**63 - 1,
) -> list[HeatPumpTraceStatus]:
    """Load candidate outcomes, including cold starts and contained failures."""
    root = Path(trace_directory)
    if not root.exists():
        return []
    results: list[HeatPumpTraceStatus] = []
    for path in sorted(root.glob("????-??-??/*.json.gz")):
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()))
            generated_at_ms = int(payload["generated_at_ms"])
            if (
                int(payload.get("schema_version", -1)) not in SUPPORTED_TRACE_SCHEMAS
                or payload.get("non_authoritative") is not True
                or not start_generated_ms <= generated_at_ms <= end_generated_ms
            ):
                continue
            candidate = (payload.get("forecast_shadows") or {}).get("heat_pump")
            if not isinstance(candidate, dict):
                continue
            status = str(candidate.get("status") or "unknown")
            forecast = candidate.get("forecast")
            if status == "completed" and isinstance(forecast, dict):
                diagnostics = forecast.get("diagnostics") or {}
                if diagnostics.get("status") == "cold_start":
                    status = "cold_start"
                else:
                    status = "ready"
            results.append(
                HeatPumpTraceStatus(
                    generated_at_ms,
                    int(payload.get("vintage_bucket_ms", generated_at_ms)),
                    status,
                )
            )
        except OSError, EOFError, UnicodeError, ValueError, TypeError, KeyError:
            continue
    return results


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


def evaluate_heat_pump_gate(
    residuals: list[Residual],
    statuses: list[HeatPumpTraceStatus],
    actuals: dict[int, dict],
    *,
    timezone: str,
) -> dict[str, object]:
    """Evaluate the pre-registered whole-heat-pump promotion gate."""
    zone = ZoneInfo(timezone)
    residual_by_key = {
        (item.series, item.generated_at_ms, item.start_ms): item for item in residuals
    }
    paired = []
    dhw_pairs = []
    for key, shadow in residual_by_key.items():
        series, generated_at_ms, start_ms = key
        if series != "shadow_heat_pump_total":
            continue
        authoritative_dhw = residual_by_key.get(
            ("load_component:heat_pump_dhw", generated_at_ms, start_ms)
        )
        authoritative_heating = residual_by_key.get(
            ("load_component:heat_pump_space_heating", generated_at_ms, start_ms)
        )
        shadow_dhw = residual_by_key.get(
            ("shadow_load_component:heat_pump_dhw", generated_at_ms, start_ms)
        )
        if authoritative_dhw is None or authoritative_heating is None:
            continue
        authoritative_error = (
            authoritative_dhw.error_kwh + authoritative_heating.error_kwh
        )
        paired.append(
            (
                start_ms,
                authoritative_error,
                shadow.error_kwh,
            )
        )
        if shadow_dhw is not None:
            dhw_pairs.append((authoritative_dhw.error_kwh, shadow_dhw.error_kwh))

    authoritative_mae = _mean_abs(item[1] for item in paired)
    shadow_mae = _mean_abs(item[2] for item in paired)
    authoritative_bias = _mean(item[1] for item in paired)
    shadow_bias = _mean(item[2] for item in paired)
    dhw_authoritative_mae = _mean_abs(item[0] for item in dhw_pairs)
    dhw_shadow_mae = _mean_abs(item[1] for item in dhw_pairs)
    confidence = _daily_block_bootstrap_mae_delta(paired, zone)
    status_counts: dict[str, int] = defaultdict(int)
    for item in statuses:
        status_counts[item.status] += 1
    experiment_statuses = [
        item for item in statuses if item.status not in {"not_configured", "unknown"}
    ]
    complete_dates = _complete_trace_dates(experiment_statuses, zone)
    complete_days = len(complete_dates)
    heating_runs = _count_complete_component_runs(
        actuals, "heat_pump_space_heating", complete_dates, zone
    )
    dhw_cycles = _count_complete_component_runs(
        actuals, "heat_pump_dhw", complete_dates, zone
    )
    configured_status_counts = {
        key: value
        for key, value in status_counts.items()
        if key not in {"not_configured", "unknown"}
    }
    configured_vintages = sum(configured_status_counts.values())
    ready_rate = (
        configured_status_counts.get("ready", 0) / configured_vintages
        if configured_vintages
        else 0.0
    )

    evidence_ready = (
        complete_days >= HEAT_PUMP_MIN_COMPLETE_DAYS
        and heating_runs >= HEAT_PUMP_MIN_RUNS
        and dhw_cycles >= HEAT_PUMP_MIN_DHW_CYCLES
        and bool(paired)
        and bool(dhw_pairs)
        and confidence is not None
        and ready_rate >= HEAT_PUMP_MIN_READY_RATE
    )
    if not evidence_ready:
        verdict = "insufficient_data"
    else:
        assert authoritative_mae is not None
        assert shadow_mae is not None
        assert authoritative_bias is not None
        assert shadow_bias is not None
        assert dhw_authoritative_mae is not None
        assert dhw_shadow_mae is not None
        assert confidence is not None
        improved = (
            shadow_mae <= authoritative_mae
            and abs(shadow_bias) <= abs(authoritative_bias)
            and confidence[1] <= 0.0
        )
        dhw_stable = (
            dhw_shadow_mae <= dhw_authoritative_mae + HEAT_PUMP_DHW_MAE_TOLERANCE_KWH
        )
        verdict = "pass" if improved and dhw_stable else "fail"

    return {
        "verdict": verdict,
        "requirements": {
            "complete_local_days": HEAT_PUMP_MIN_COMPLETE_DAYS,
            "space_heating_runs": HEAT_PUMP_MIN_RUNS,
            "dhw_cycles": HEAT_PUMP_MIN_DHW_CYCLES,
            "dhw_mae_regression_tolerance_kwh": (HEAT_PUMP_DHW_MAE_TOLERANCE_KWH),
            "minimum_ready_rate_pct": 100.0 * HEAT_PUMP_MIN_READY_RATE,
        },
        "evidence": {
            "complete_local_days": complete_days,
            "space_heating_runs": heating_runs,
            "dhw_cycles": dhw_cycles,
            "paired_slots": len(paired),
            "dhw_paired_slots": len(dhw_pairs),
            "candidate_status_counts": dict(sorted(status_counts.items())),
            "configured_candidate_status_rates_pct": {
                key: 100.0 * value / configured_vintages
                for key, value in sorted(configured_status_counts.items())
            }
            if configured_vintages
            else {},
        },
        "paired_metrics": {
            "authoritative_mae_kwh": authoritative_mae,
            "shadow_mae_kwh": shadow_mae,
            "authoritative_bias_kwh": authoritative_bias,
            "shadow_bias_kwh": shadow_bias,
            "shadow_minus_authoritative_mae_95pct_ci_kwh": confidence,
            "authoritative_dhw_mae_kwh": dhw_authoritative_mae,
            "shadow_dhw_mae_kwh": dhw_shadow_mae,
        },
    }


def _complete_trace_dates(statuses, zone):
    buckets_by_day: dict[datetime.date, set[int]] = defaultdict(set)
    for item in statuses:
        local = datetime.fromtimestamp(item.vintage_bucket_ms / 1000.0, UTC).astimezone(
            zone
        )
        buckets_by_day[local.date()].add(item.vintage_bucket_ms)
    complete = set()
    for local_day, buckets in buckets_by_day.items():
        local_start = datetime.combine(local_day, datetime.min.time(), zone)
        local_end = datetime.combine(
            local_day + timedelta(days=1), datetime.min.time(), zone
        )
        expected = int((local_end.timestamp() - local_start.timestamp()) / (15 * 60))
        if len(buckets) == expected:
            complete.add(local_day)
    return complete


def _count_complete_component_runs(actuals, component_key, complete_dates, zone):
    runs = 0
    for local_day in complete_dates:
        local_start = datetime.combine(local_day, datetime.min.time(), zone)
        local_end = datetime.combine(
            local_day + timedelta(days=1), datetime.min.time(), zone
        )
        start_ms = int(local_start.timestamp() * 1000)
        end_ms = int(local_end.timestamp() * 1000)
        energies = []
        for slot_start_ms in range(start_ms, end_ms, SLOT_MS):
            energy = _actual_component_value(actuals.get(slot_start_ms), component_key)
            if energy is None:
                energies = []
                break
            energies.append(energy)
        if not energies:
            continue
        active = [value >= HEAT_PUMP_ACTIVE_KWH for value in energies]
        runs += sum(
            not active[index - 1]
            and active[index]
            and any(not value for value in active[index + 1 :])
            for index in range(1, len(active) - 1)
        )
    return runs


def _daily_block_bootstrap_mae_delta(paired, zone):
    by_day: dict[datetime.date, list[float]] = defaultdict(list)
    for start_ms, authoritative_error, shadow_error in paired:
        local_day = (
            datetime.fromtimestamp(start_ms / 1000.0, UTC).astimezone(zone).date()
        )
        by_day[local_day].append(abs(shadow_error) - abs(authoritative_error))
    day_means = [sum(values) / len(values) for values in by_day.values() if values]
    if len(day_means) < 2:
        return None
    rng = random.Random(0)
    samples = sorted(
        sum(rng.choice(day_means) for _ in day_means) / len(day_means)
        for _ in range(2000)
    )
    return [samples[49], samples[1949]]


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def _mean_abs(values):
    return _mean(abs(value) for value in values)


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
    if series == "shadow_heat_pump_total":
        values = [
            _actual_component_value(actual, key)
            for key in ("heat_pump_dhw", "heat_pump_space_heating")
        ]
        return None if any(value is None for value in values) else sum(values)
    if series.startswith("shadow_load_component:"):
        return _actual_component_value(
            actual, series.removeprefix("shadow_load_component:")
        )
    if not series.startswith("load_component:"):
        return None
    return _actual_component_value(actual, series.removeprefix("load_component:"))


def _actual_component_value(actual: dict, key: str) -> float | None:
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
    parser.add_argument("--timezone", default="UTC")
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
    actuals = load_actuals(args.feature_store)
    residuals = compare_forecasts(observations, actuals, as_of_ms=as_of_ms)
    rows = summarize(residuals)
    statuses = load_heat_pump_trace_statuses(
        args.trace_dir,
        start_generated_ms=start_ms,
        end_generated_ms=as_of_ms,
    )
    report = {
        "non_authoritative": True,
        "as_of_ms": as_of_ms,
        "window_days": args.days,
        "forecast_vintages": len({item.generated_at_ms for item in observations}),
        "matured_comparisons": len(residuals),
        "metrics": rows,
        "heat_pump_shadow_gate": evaluate_heat_pump_gate(
            residuals,
            statuses,
            actuals,
            timezone=args.timezone,
        ),
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

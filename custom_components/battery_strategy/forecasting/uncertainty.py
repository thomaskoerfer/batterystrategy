"""Empirical uncertainty from matured production-forecast residuals."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..contracts import QuantileEnergy

MIN_CALIBRATION_SAMPLES = 12
UNCERTAINTY_MODEL_VERSION = "empirical-residual-q1"
LEAD_BUCKETS_MS = (60 * 60 * 1000, 6 * 60 * 60 * 1000, 24 * 60 * 60 * 1000)
TOTAL_LOAD_SERIES = "@total_load"
PV_SERIES = "@pv"


def component_series(component_key: str) -> str:
    """Namespace configured component keys away from reserved totals."""
    return f"component:{component_key}"


def lead_bucket(generated_at_ms: int, target_start_ms: int) -> int:
    """Classify one issued forecast by target lead time."""
    lead_ms = max(0, target_start_ms - generated_at_ms)
    return sum(lead_ms >= boundary for boundary in LEAD_BUCKETS_MS)


def base_model_version(model_version: str) -> str:
    """Keep uncertainty revisions separate from the calibrated point model."""
    return model_version.split(f"+{UNCERTAINTY_MODEL_VERSION}", 1)[0]


def cohort_key(
    series_key: str,
    model_version: str,
    bucket: int,
    is_weekend: bool | None = None,
    predicted_active: bool | None = None,
) -> str:
    """Encode a compact, JSON-safe calibration cohort identity."""
    weektype = "*" if is_weekend is None else ("w" if is_weekend else "d")
    activity = (
        "*" if predicted_active is None else ("on" if predicted_active else "off")
    )
    return "\x1f".join(
        (series_key, base_model_version(model_version), str(bucket), weektype, activity)
    )


@dataclass(frozen=True, slots=True)
class ForecastResidualCalibration:
    """Immutable lookup over forecast-owned matured residual cohorts."""

    cohorts: dict[str, tuple[float, ...]]

    @classmethod
    def from_persisted(
        cls, cohorts: dict[str, list[float]]
    ) -> ForecastResidualCalibration:
        normalized = {}
        for key, values in cohorts.items():
            if not isinstance(values, list):
                continue
            finite = []
            for value in values:
                try:
                    number = float(value)
                except TypeError, ValueError:
                    continue
                if math.isfinite(number):
                    finite.append(number)
            normalized[str(key)] = tuple(finite)
        return cls(normalized)

    def residuals_for(
        self,
        series_key: str,
        model_version: str,
        generated_at_ms: int,
        target_start_ms: int,
        is_weekend: bool,
        predicted_active: bool,
    ) -> tuple[float, ...]:
        """Prefer the narrowest mature cohort, then broaden deterministically."""
        bucket = lead_bucket(generated_at_ms, target_start_ms)
        keys = (
            cohort_key(series_key, model_version, bucket, is_weekend, predicted_active),
            cohort_key(series_key, model_version, bucket, None, predicted_active),
            cohort_key(series_key, model_version, bucket),
        )
        for key in keys:
            values = self.cohorts.get(key, ())
            if len(values) >= MIN_CALIBRATION_SAMPLES:
                return values
        return ()


EMPTY_CALIBRATION = ForecastResidualCalibration({})


def calibrated_quantile_energy(
    p50_kwh: float,
    residuals_kwh: tuple[float, ...],
    *,
    minimum_samples: int = MIN_CALIBRATION_SAMPLES,
) -> QuantileEnergy:
    """Anchor an empirical production-error spread to unchanged P50."""
    values = tuple(
        sorted(float(value) for value in residuals_kwh if math.isfinite(float(value)))
    )
    if len(values) < minimum_samples:
        return QuantileEnergy(p50_kwh)
    lower = max(0.0, p50_kwh + _quantile(values, 0.1))
    upper = max(p50_kwh, p50_kwh + _quantile(values, 0.9))
    return QuantileEnergy(
        p50_kwh=p50_kwh,
        p10_kwh=min(p50_kwh, lower),
        p90_kwh=upper,
        calibration_samples=len(values),
    )


def uncertainty_model_version(point_model_version: str) -> str:
    return f"{base_model_version(point_model_version)}+{UNCERTAINTY_MODEL_VERSION}"


def _quantile(values: tuple[float, ...], probability: float) -> float:
    position = (len(values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return values[lower_index]
    fraction = position - lower_index
    return values[lower_index] * (1.0 - fraction) + values[upper_index] * fraction

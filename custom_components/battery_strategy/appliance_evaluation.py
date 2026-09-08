"""Walk-forward evaluation for finite cyclic appliance forecasts."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

from .contracts import LoadDriverSnapshot, LoadFeatureValue
from .forecasting.cyclic_appliance import (
    MAX_CYCLE_SLOTS,
    appliance_cycles,
    forecast_cyclic_appliance,
)


@dataclass(frozen=True, slots=True)
class ApplianceCycleEvaluation:
    """One forecast made after the first finalized active slot."""

    start_ms: int
    actual_remaining_kwh: float
    predicted_remaining_kwh: float
    remaining_energy_error_kwh: float
    slot_mae_kwh: float
    end_slot_error: int
    context_predicted_remaining_kwh: float
    context_remaining_energy_error_kwh: float
    context_slot_mae_kwh: float
    context_end_slot_error: int


@dataclass(frozen=True, slots=True)
class ApplianceBacktestReport:
    """Aggregate walk-forward quality for one component key."""

    component_key: str
    completed_cycles: int
    evaluated_cycles: int
    skipped_warmup_cycles: int
    remaining_energy_mae_kwh: float | None
    slot_mae_kwh: float | None
    end_slot_mae: float | None
    context_remaining_energy_mae_kwh: float | None
    context_slot_mae_kwh: float | None
    context_end_slot_mae: float | None
    cycles: tuple[ApplianceCycleEvaluation, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable report."""
        return asdict(self)


def evaluate_cyclic_appliance(history, component_key: str) -> ApplianceBacktestReport:
    """Replay each mature cycle using only information available at that time."""
    cycles, _active_tail = appliance_cycles(tuple(history), component_key)
    evaluations = []
    for cycle in cycles:
        first_slot_end = cycle.start_ms + 900_000
        prior = tuple(item for item in history if item.slot.end_ms <= first_slot_end)
        prior_cycles, _ = appliance_cycles(prior, component_key)
        # The current event is unfinished in ``prior`` and is not part of this count.
        if len(prior_cycles) < 3:
            continue
        first_energy = cycle.energy_kwh[0]
        power_only_driver = LoadDriverSnapshot(
            component_key,
            first_energy * 4_000.0,
            features=(LoadFeatureValue("cycle_active_fraction", 1.0),),
        )
        first_component = next(
            item
            for item in prior[-1].load_components
            if item.component_key == component_key
        )
        context_features = tuple(
            feature
            for feature in first_component.features
            if feature.feature_key != "cycle_active_fraction"
            and feature.quality.coverage >= 0.999
            and not feature.quality.flags
        )
        context_driver = LoadDriverSnapshot(
            component_key,
            first_energy * 4_000.0,
            features=(
                LoadFeatureValue("cycle_active_fraction", 1.0),
                *context_features,
            ),
        )
        predicted = forecast_cyclic_appliance(
            prior, power_only_driver, component_key, MAX_CYCLE_SLOTS
        ).energy_kwh
        context_predicted = forecast_cyclic_appliance(
            prior, context_driver, component_key, MAX_CYCLE_SLOTS
        ).energy_kwh
        actual = cycle.energy_kwh[1:]
        predicted_remaining, energy_error, slot_mae, end_error = _cycle_errors(
            predicted, actual
        )
        context_remaining, context_error, context_slot_mae, context_end_error = (
            _cycle_errors(context_predicted, actual)
        )
        actual_remaining = sum(actual)
        evaluations.append(
            ApplianceCycleEvaluation(
                cycle.start_ms,
                actual_remaining,
                predicted_remaining,
                energy_error,
                slot_mae,
                end_error,
                context_remaining,
                context_error,
                context_slot_mae,
                context_end_error,
            )
        )

    return ApplianceBacktestReport(
        component_key,
        len(cycles),
        len(evaluations),
        len(cycles) - len(evaluations),
        _mean_abs(item.remaining_energy_error_kwh for item in evaluations),
        _mean(item.slot_mae_kwh for item in evaluations),
        _mean_abs(item.end_slot_error for item in evaluations),
        _mean_abs(item.context_remaining_energy_error_kwh for item in evaluations),
        _mean(item.context_slot_mae_kwh for item in evaluations),
        _mean_abs(item.context_end_slot_error for item in evaluations),
        tuple(evaluations),
    )


def _cycle_errors(predicted, actual) -> tuple[float, float, float, int]:
    predicted_end = next(
        (
            index + 1
            for index in range(len(predicted) - 1, -1, -1)
            if predicted[index] > 0
        ),
        0,
    )
    horizon = max(len(actual), predicted_end)
    actual_aligned = (*actual, *((0.0,) * max(0, horizon - len(actual))))
    predicted_aligned = (*predicted[:horizon],)
    if len(predicted_aligned) < horizon:
        predicted_aligned += (0.0,) * (horizon - len(predicted_aligned))
    slot_errors = [
        abs(expected - observed)
        for expected, observed in zip(predicted_aligned, actual_aligned, strict=True)
    ]
    predicted_remaining = sum(predicted)
    actual_remaining = sum(actual)
    return (
        predicted_remaining,
        predicted_remaining - actual_remaining,
        statistics.mean(slot_errors) if slot_errors else 0.0,
        predicted_end - len(actual),
    )


def _mean(values) -> float | None:
    materialized = tuple(float(value) for value in values)
    return statistics.mean(materialized) if materialized else None


def _mean_abs(values) -> float | None:
    return _mean(abs(float(value)) for value in values)

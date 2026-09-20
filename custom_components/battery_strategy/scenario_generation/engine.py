"""Dependence-aware empirical scenario generation."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..contracts import (
    HistoricalFeatureSlot,
    QualityFlag,
    ScenarioBuildDiagnostics,
    ScenarioBuildRequest,
    ScenarioBuildResult,
    ScenarioBuildStatus,
    ScenarioBundle,
    ScenarioCandidateDiagnostic,
    ScenarioPath,
    ScenarioSlot,
)
from ..forecasting.uncertainty import (
    PV_SERIES,
    TOTAL_LOAD_SERIES,
    cohort_key,
    lead_bucket,
)

MODEL_VERSION = "weekly-empirical-copula-v3"
REPAIR_POLICY_VERSION = "isolated-continuous-2pct-v1"
_LOAD_INVALID = frozenset(
    {QualityFlag.MISSING_GRID, QualityFlag.MISSING_BATTERY, QualityFlag.RESTART_GAP}
)
_PV_INVALID = frozenset({QualityFlag.MISSING_PV, QualityFlag.RESTART_GAP})
_EV_INVALID = frozenset({QualityFlag.MISSING_EV, QualityFlag.RESTART_GAP})


@dataclass(frozen=True, slots=True)
class _Candidate:
    weeks_ago: int
    slots: tuple[HistoricalFeatureSlot, ...]
    load_template: tuple[float, ...]
    pv_template: tuple[float, ...]
    repaired_slots: int
    excluded_components: int
    component_distance: float


class ScenarioBuilder:
    """Build bounded joint paths without optimization or runtime dependencies."""

    def build(self, request: ScenarioBuildRequest) -> ScenarioBuildResult:
        forecast = request.forecast
        evidence = request.evidence
        generated_at = max(
            forecast.load.generated_at_ms,
            forecast.pv.generated_at_ms,
            forecast.ev.generated_at_ms,
        )
        earliest_forecast_at = min(
            forecast.load.generated_at_ms,
            forecast.pv.generated_at_ms,
            forecast.ev.generated_at_ms,
        )
        fingerprint = _fingerprint(request)
        reasons: Counter[str] = Counter()
        marginal_sources: Counter[str] = Counter()
        input_valid = bool(
            forecast.load.slots
            and evidence.training_cutoff_ms <= earliest_forecast_at
            and evidence.captured_at_ms <= earliest_forecast_at
        )
        eligible = False

        fallback_slots = 0
        ev_probability_max_error = 0.0
        candidate_diagnostics: tuple[ScenarioCandidateDiagnostic, ...] = ()

        def diagnostics(accepted, candidates, repaired, excluded):
            return ScenarioBuildDiagnostics(
                eligible,
                candidates,
                accepted,
                repaired,
                fallback_slots,
                tuple(sorted(marginal_sources.items())),
                ev_probability_max_error,
                excluded,
                tuple(sorted(reasons.items())),
                evidence.evidence_id,
                evidence.training_cutoff_ms,
                request.settings.seed,
                fingerprint,
                f"{MODEL_VERSION}+{REPAIR_POLICY_VERSION}",
                candidate_diagnostics,
            )

        if not input_valid:
            reasons["ineligible_vintage"] += 1
            return ScenarioBuildResult(
                None, ScenarioBuildStatus.INVALID_INPUT, diagnostics(0, 0, 0, 0)
            )

        try:
            zone = ZoneInfo(evidence.timezone)
        except ZoneInfoNotFoundError:
            reasons["invalid_timezone"] += 1
            return ScenarioBuildResult(
                None, ScenarioBuildStatus.INVALID_INPUT, diagnostics(0, 0, 0, 0)
            )
        history = tuple(
            item
            for item in evidence.history
            if item.slot.end_ms <= evidence.training_cutoff_ms
        )
        by_local = {_local_key(item.slot.start_ms, zone): item for item in history}
        targets = tuple(
            dt.datetime.fromtimestamp(slot.slot.start_ms / 1000.0, dt.UTC).astimezone(
                zone
            )
            for slot in forecast.load.slots
        )
        active_now = forecast.ev.slots[0].active_probability >= 0.5
        candidates = []
        overlapping_paths = 0
        for weeks_ago in range(1, 54):
            raw = tuple(
                by_local.get(_shifted_key(local, weeks_ago)) for local in targets
            )
            overlapping_paths += int(
                bool(raw) and all(item is not None for item in raw)
            )
            candidate = _prepare_candidate(
                weeks_ago,
                raw,
                request.settings.maximum_repair_fraction,
                active_now,
                evidence.ev_active_threshold_kwh,
                forecast.load.components,
                reasons,
            )
            if candidate is not None:
                candidates.append(candidate)
        eligible = overlapping_paths >= request.settings.minimum_paths
        candidates.sort(key=lambda item: (item.component_distance, item.weeks_ago))
        all_candidates = tuple(candidates)
        candidates = candidates[: request.settings.maximum_paths]
        if len(candidates) < request.settings.minimum_paths:
            reasons["too_few_paths"] += 1
            return ScenarioBuildResult(
                None,
                ScenarioBuildStatus.INSUFFICIENT_EVIDENCE,
                diagnostics(
                    0,
                    len(candidates),
                    sum(item.repaired_slots for item in candidates),
                    sum(item.excluded_components for item in candidates),
                ),
            )

        cohort_map = {item.key: item.values_kwh for item in evidence.residual_cohorts}
        marginal_evidence = []
        for slot_index, (load_slot, pv_slot) in enumerate(
            zip(forecast.load.slots, forecast.pv.slots, strict=True)
        ):
            local = targets[slot_index]
            load_residuals = _residuals(
                cohort_map,
                TOTAL_LOAD_SERIES,
                forecast.load.model_version,
                generated_at,
                load_slot.slot.start_ms,
                local.weekday() >= 5,
                load_slot.energy.p50_kwh > 1e-9,
            )
            pv_residuals = _residuals(
                cohort_map,
                PV_SERIES,
                forecast.pv.model_version,
                generated_at,
                pv_slot.slot.start_ms,
                local.weekday() >= 5,
                pv_slot.energy.p50_kwh > 1e-9,
            )
            load_mode = _marginal_mode(load_slot.energy, load_residuals)
            pv_mode = _marginal_mode(pv_slot.energy, pv_residuals)
            marginal_sources[f"load:{load_mode}"] += 1
            marginal_sources[f"pv:{pv_mode}"] += 1
            fallback_slots += int(load_mode != "calibrated_residual")
            fallback_slots += int(pv_mode != "calibrated_residual")
            marginal_evidence.append((load_residuals, pv_residuals, load_mode, pv_mode))
        raw_weights = [
            max(1e-9, 1.0 - item.repaired_slots / max(1, len(item.slots)))
            / (1.0 + item.component_distance)
            for item in candidates
        ]
        total_weight = sum(raw_weights)
        probabilities = tuple(weight / total_weight for weight in raw_weights)
        selected_weeks = {item.weeks_ago for item in candidates}
        probability_by_week = {
            item.weeks_ago: probabilities[index]
            for index, item in enumerate(candidates)
        }
        candidate_diagnostics = tuple(
            ScenarioCandidateDiagnostic(
                item.weeks_ago,
                item.component_distance,
                item.repaired_slots,
                item.excluded_components,
                max(
                    1e-9,
                    1.0 - item.repaired_slots / max(1, len(item.slots)),
                )
                / (1.0 + item.component_distance),
                probability_by_week.get(item.weeks_ago, 0.0),
                item.weeks_ago in selected_weeks,
            )
            for item in all_candidates
        )
        ev_active_paths = []
        for slot_index, ev_slot in enumerate(forecast.ev.slots):
            historical_values = tuple(
                item.slots[slot_index].ev_charge_kwh for item in candidates
            )
            active = _weighted_active_tail(
                historical_values,
                probabilities,
                ev_slot.active_probability,
            )
            ev_active_paths.append(active)
            realized_probability = sum(probabilities[index] for index in active)
            ev_probability_max_error = max(
                ev_probability_max_error,
                abs(realized_probability - ev_slot.active_probability),
            )
        paths = []
        for index, (candidate, raw_weight) in enumerate(
            zip(candidates, raw_weights, strict=True)
        ):
            path_slots = []
            for slot_index, historical in enumerate(candidate.slots):
                load_values = tuple(
                    item.load_template[slot_index] for item in candidates
                )
                pv_values = tuple(item.pv_template[slot_index] for item in candidates)
                load_slot = forecast.load.slots[slot_index]
                pv_slot = forecast.pv.slots[slot_index]
                ev_slot = forecast.ev.slots[slot_index]
                load_residuals, pv_residuals, load_mode, pv_mode = marginal_evidence[
                    slot_index
                ]
                load = _mapped_marginal(
                    load_slot.energy,
                    load_residuals,
                    load_values,
                    _rank(candidate.load_template[slot_index], load_values),
                    load_mode,
                )
                pv = min(
                    evidence.pv_slot_cap_kwh,
                    _mapped_marginal(
                        pv_slot.energy,
                        pv_residuals,
                        pv_values,
                        _rank(candidate.pv_template[slot_index], pv_values),
                        pv_mode,
                    ),
                )
                active_indexes = ev_active_paths[slot_index]
                if index not in active_indexes:
                    ev = 0.0
                else:
                    active_values = tuple(
                        candidates[item].slots[slot_index].ev_charge_kwh
                        for item in active_indexes
                    )
                    ev = max(
                        evidence.ev_active_threshold_kwh,
                        _marginal_value(
                            ev_slot.energy,
                            _rank(historical.ev_charge_kwh, active_values),
                        ),
                    )
                path_slots.append(
                    ScenarioSlot(
                        load_slot.slot,
                        round(load, 6),
                        round(pv, 6),
                        round(max(0.0, ev), 6),
                    )
                )
            paths.append(
                ScenarioPath(
                    f"week-{candidate.weeks_ago}-{index}",
                    raw_weight / total_weight,
                    tuple(path_slots),
                )
            )

        bundle = ScenarioBundle(
            f"{generated_at}:{MODEL_VERSION}",
            f"{forecast.load.forecast_id}|{forecast.pv.forecast_id}|{forecast.ev.forecast_id}",
            generated_at,
            evidence.training_cutoff_ms,
            MODEL_VERSION,
            tuple(paths),
        )
        return ScenarioBuildResult(
            bundle,
            ScenarioBuildStatus.COMPLETED,
            diagnostics(
                len(paths),
                len(candidates),
                sum(item.repaired_slots for item in candidates),
                sum(item.excluded_components for item in candidates),
            ),
        )


def _prepare_candidate(
    weeks_ago: int,
    raw: tuple[HistoricalFeatureSlot | None, ...],
    maximum_repair_fraction: float,
    active_now: bool,
    ev_active_threshold_kwh: float,
    forecast_components,
    reasons: Counter[str],
) -> _Candidate | None:
    if not raw or any(item is None for item in raw):
        reasons["missing_slot"] += 1
        return None
    slots = tuple(item for item in raw if item is not None)
    if active_now and slots[0].ev_charge_kwh < ev_active_threshold_kwh:
        reasons["ev_state_mismatch"] += 1
        return None
    if any(frozenset(item.quality.flags) & _EV_INVALID for item in slots):
        reasons["invalid_ev_boundary"] += 1
        return None
    max_repairs = math.floor(len(slots) * maximum_repair_fraction)
    load_values, load_repairs = _repair_continuous(
        tuple(item.house_load_no_ev_kwh for item in slots),
        tuple(_valid(item, _LOAD_INVALID) for item in slots),
        max_repairs,
    )
    pv_values, pv_repairs = _repair_continuous(
        tuple(item.pv_generation_kwh for item in slots),
        tuple(_valid(item, _PV_INVALID) for item in slots),
        max_repairs,
    )
    if (
        load_values is None
        or pv_values is None
        or load_repairs + pv_repairs > max_repairs
    ):
        reasons["repair_limit"] += 1
        return None
    component_distance, excluded = _component_distance(
        slots,
        forecast_components,
    )
    return _Candidate(
        weeks_ago,
        slots,
        load_values,
        pv_values,
        load_repairs + pv_repairs,
        excluded,
        component_distance,
    )


def _repair_continuous(values, valid, max_repairs):
    invalid = [index for index, usable in enumerate(valid) if not usable]
    if len(invalid) > max_repairs:
        return None, 0
    result = list(values)
    for index in invalid:
        if (
            index == 0
            or index == len(values) - 1
            or not valid[index - 1]
            or not valid[index + 1]
        ):
            return None, 0
        result[index] = (values[index - 1] + values[index + 1]) / 2.0
    return tuple(result), len(invalid)


def _valid(item: HistoricalFeatureSlot, invalid_flags: frozenset[QualityFlag]) -> bool:
    return item.quality.coverage >= 0.999 and not (
        frozenset(item.quality.flags) & invalid_flags
    )


def _residuals(cohorts, series, version, generated, target, weekend, active):
    bucket = lead_bucket(generated, target)
    for key in (
        cohort_key(series, version, bucket, weekend, active),
        cohort_key(series, version, bucket, None, active),
        cohort_key(series, version, bucket),
    ):
        values = cohorts.get(key, ())
        if len(values) >= 12:
            return values
    return ()


def _marginal_value(energy, probability):
    if energy.p10_kwh is None or energy.p90_kwh is None:
        return energy.p50_kwh
    if probability <= 0.1:
        return energy.p10_kwh
    if probability <= 0.5:
        fraction = (probability - 0.1) / 0.4
        return energy.p10_kwh + fraction * (energy.p50_kwh - energy.p10_kwh)
    if probability >= 0.9:
        return energy.p90_kwh
    fraction = (probability - 0.5) / 0.4
    return energy.p50_kwh + fraction * (energy.p90_kwh - energy.p50_kwh)


def _weighted_active_tail(values, probabilities, target_probability):
    selected = set()
    selected_probability = 0.0
    for index in sorted(
        range(len(values)), key=lambda item: (values[item], item), reverse=True
    ):
        candidate_probability = selected_probability + probabilities[index]
        if abs(candidate_probability - target_probability) > abs(
            selected_probability - target_probability
        ):
            continue
        selected.add(index)
        selected_probability = candidate_probability
    return frozenset(selected)


def _component_distance(history, forecast_components):
    if not forecast_components:
        return 0.0, sum(
            component.quality.coverage < 0.999 or bool(component.quality.flags)
            for item in history
            for component in item.load_components
        )
    distances = []
    excluded = 0
    for component in forecast_components:
        for index, forecast_slot in enumerate(component.slots):
            historical = next(
                (
                    item
                    for item in history[index].load_components
                    if item.component_key == component.component_key
                ),
                None,
            )
            if (
                historical is None
                or historical.quality.coverage < 0.999
                or historical.quality.flags
            ):
                excluded += 1
                continue
            scale = max(
                0.05,
                forecast_slot.energy.p50_kwh,
                historical.energy_kwh,
            )
            distances.append(
                abs(historical.energy_kwh - forecast_slot.energy.p50_kwh) / scale
            )
    return (sum(distances) / len(distances) if distances else 0.0), excluded


def _marginal_mode(energy, residuals: tuple[float, ...]) -> str:
    if residuals:
        return "calibrated_residual"
    if energy.p10_kwh is not None and energy.p90_kwh is not None:
        return "forecast_quantiles"
    return "centered_empirical"


def _mapped_marginal(energy, residuals, historical_values, probability, mode):
    if mode == "calibrated_residual":
        value = energy.p50_kwh + _quantile(residuals, probability)
    elif mode == "forecast_quantiles":
        value = _marginal_value(energy, probability)
    else:
        value = energy.p50_kwh + (
            _quantile(historical_values, probability)
            - _quantile(historical_values, 0.5)
        )
    return max(0.0, value)


def _rank(value: float, values: tuple[float, ...]) -> float:
    lower = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    return (lower + 0.5 * max(1, equal)) / len(values)


def _quantile(values: tuple[float, ...], probability: float) -> float:
    ordered = tuple(sorted(values))
    position = (len(ordered) - 1) * max(0.0, min(1.0, probability))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _fingerprint(request: ScenarioBuildRequest) -> str:
    payload = (
        request.forecast.load.forecast_id,
        request.forecast.pv.forecast_id,
        request.forecast.ev.forecast_id,
        request.evidence.evidence_id,
        request.evidence.training_cutoff_ms,
        request.evidence.pv_slot_cap_kwh,
        request.evidence.ev_active_threshold_kwh,
        request.settings.minimum_paths,
        request.settings.maximum_paths,
        request.settings.maximum_repair_fraction,
        request.settings.seed,
        MODEL_VERSION,
        REPAIR_POLICY_VERSION,
    )
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def _shifted_key(local: dt.datetime, weeks_ago: int) -> tuple[str, int, int, int]:
    shifted = local - dt.timedelta(weeks=weeks_ago)
    return shifted.date().isoformat(), shifted.hour, shifted.minute, shifted.fold


def _local_key(timestamp_ms: int, zone: ZoneInfo) -> tuple[str, int, int, int]:
    local = dt.datetime.fromtimestamp(timestamp_ms / 1000.0, dt.UTC).astimezone(zone)
    return local.date().isoformat(), local.hour, local.minute, local.fold

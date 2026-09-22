"""Contracts for dependence-aware scenario generation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .common import (
    SlotKey,
    require_finite,
    require_nonnegative,
    require_slots_sorted_unique,
)
from .forecasting import ForecastDistributionBundle, HistoricalFeatureSlot

MAX_SCENARIOS = 12


@dataclass(frozen=True, slots=True)
class ResidualCohort:
    """Immutable causal residual evidence for one forecast cohort."""

    key: str
    values_kwh: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ScenarioEvidenceSnapshot:
    """Causal joint history and marginal evidence captured for one vintage."""

    evidence_id: str
    captured_at_ms: int
    training_cutoff_ms: int
    timezone: str
    history: tuple[HistoricalFeatureSlot, ...]
    residual_cohorts: tuple[ResidualCohort, ...]
    pv_slot_cap_kwh: float
    ev_active_threshold_kwh: float

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.timezone:
            raise ValueError("scenario evidence identity and timezone are required")
        if self.training_cutoff_ms > self.captured_at_ms:
            raise ValueError("scenario evidence cannot use future training data")
        if any(item.slot.end_ms > self.training_cutoff_ms for item in self.history):
            raise ValueError("scenario history cannot exceed its training cutoff")
        require_nonnegative("pv_slot_cap_kwh", self.pv_slot_cap_kwh)
        require_nonnegative("ev_active_threshold_kwh", self.ev_active_threshold_kwh)
        if len({item.key for item in self.residual_cohorts}) != len(
            self.residual_cohorts
        ):
            raise ValueError("scenario residual cohort keys must be unique")
        if len({item.slot for item in self.history}) != len(self.history):
            raise ValueError("scenario evidence history slots must be unique")
        if any(
            not math.isfinite(value)
            for cohort in self.residual_cohorts
            for value in cohort.values_kwh
        ):
            raise ValueError("scenario residual evidence must be finite")


@dataclass(frozen=True, slots=True)
class ScenarioGenerationSettings:
    minimum_paths: int = 4
    maximum_paths: int = 12
    maximum_repair_fraction: float = 0.02
    seed: int = 0

    def __post_init__(self) -> None:
        if self.minimum_paths <= 0 or not (
            self.minimum_paths <= self.maximum_paths <= MAX_SCENARIOS
        ):
            raise ValueError("scenario path limits are invalid")
        if not 0.0 <= self.maximum_repair_fraction <= 0.05:
            raise ValueError("scenario repair fraction must be in [0, 0.05]")


@dataclass(frozen=True, slots=True)
class ScenarioMarketSlot:
    """One market observation supplied to scenario generation."""

    slot: SlotKey
    import_price_ct_per_kwh: float
    export_price_ct_per_kwh: float
    source: str

    def __post_init__(self) -> None:
        require_finite("import_price_ct_per_kwh", self.import_price_ct_per_kwh)
        require_finite("export_price_ct_per_kwh", self.export_price_ct_per_kwh)
        if not self.source:
            raise ValueError("scenario market source is required")


@dataclass(frozen=True, slots=True)
class ScenarioBuildRequest:
    """Complete immutable input to scenario generation."""

    forecast: ForecastDistributionBundle
    evidence: ScenarioEvidenceSnapshot
    settings: ScenarioGenerationSettings = ScenarioGenerationSettings()
    market: tuple[ScenarioMarketSlot, ...] = ()

    def __post_init__(self) -> None:
        if self.market:
            require_slots_sorted_unique(tuple(item.slot for item in self.market))
            if tuple(item.slot for item in self.market) != tuple(
                item.slot for item in self.forecast.load.slots
            ):
                raise ValueError("scenario market and forecast grids must match")


@dataclass(frozen=True, slots=True)
class ScenarioSlot:
    slot: SlotKey
    house_load_kwh: float
    pv_generation_kwh: float
    ev_charge_kwh: float
    import_price_ct_per_kwh: float | None = None
    export_price_ct_per_kwh: float | None = None
    price_is_firm: bool = True

    def __post_init__(self) -> None:
        require_nonnegative("house_load_kwh", self.house_load_kwh)
        require_nonnegative("pv_generation_kwh", self.pv_generation_kwh)
        require_nonnegative("ev_charge_kwh", self.ev_charge_kwh)
        if self.import_price_ct_per_kwh is not None:
            require_finite("import_price_ct_per_kwh", self.import_price_ct_per_kwh)
        if self.export_price_ct_per_kwh is not None:
            require_finite("export_price_ct_per_kwh", self.export_price_ct_per_kwh)


@dataclass(frozen=True, slots=True)
class ScenarioPath:
    scenario_id: str
    probability: float
    slots: tuple[ScenarioSlot, ...]

    def __post_init__(self) -> None:
        if not self.scenario_id or not 0.0 < self.probability <= 1.0:
            raise ValueError("scenario identity and positive probability are required")
        require_slots_sorted_unique(tuple(item.slot for item in self.slots))


@dataclass(frozen=True, slots=True)
class ScenarioBundle:
    scenario_set_id: str
    source_forecast_id: str
    generated_at_ms: int
    training_cutoff_ms: int
    model_version: str
    scenarios: tuple[ScenarioPath, ...]

    def __post_init__(self) -> None:
        if (
            not self.scenario_set_id
            or not self.source_forecast_id
            or not self.model_version
        ):
            raise ValueError("scenario identity is required")
        if self.training_cutoff_ms > self.generated_at_ms or not self.scenarios:
            raise ValueError("scenario timestamps and paths are invalid")
        if len(self.scenarios) > MAX_SCENARIOS:
            raise ValueError("scenario bundle exceeds path limit")
        if len({item.scenario_id for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("scenario path ids must be unique")
        if abs(sum(item.probability for item in self.scenarios) - 1.0) > 1e-9:
            raise ValueError("scenario probabilities must sum to one")
        grid = tuple(item.slot for item in self.scenarios[0].slots)
        if any(
            tuple(item.slot for item in path.slots) != grid for path in self.scenarios
        ):
            raise ValueError("scenario paths must use one grid")


class ScenarioBuildStatus(StrEnum):
    COMPLETED = "completed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_INPUT = "invalid_input"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScenarioCandidateDiagnostic:
    """Compact, non-authoritative evidence for offline scenario evaluation."""

    weeks_ago: int
    component_distance: float
    repaired_slots: int
    excluded_component_values: int
    raw_weight: float
    probability: float
    selected: bool


@dataclass(frozen=True, slots=True)
class ScenarioBuildDiagnostics:
    eligible: bool
    candidate_paths: int
    accepted_paths: int
    repaired_slots: int
    fallback_marginal_slots: int
    marginal_sources: tuple[tuple[str, int], ...]
    ev_probability_max_error: float
    excluded_component_values: int
    rejected_reasons: tuple[tuple[str, int], ...]
    evidence_id: str
    training_cutoff_ms: int
    seed: int
    input_fingerprint: str
    model_version: str
    candidates: tuple[ScenarioCandidateDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioBuildResult:
    scenarios: ScenarioBundle | None
    status: ScenarioBuildStatus
    diagnostics: ScenarioBuildDiagnostics

"""Short-horizon forecasts for finite, separately metered appliance cycles."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from ..contracts import HistoricalFeatureSlot, LoadDriverSnapshot

SLOT_S = 900.0
ACTIVE_POWER_W = 20.0
ACTIVE_ENERGY_KWH = ACTIVE_POWER_W * SLOT_S / 3_600_000.0
MAX_INTERNAL_GAP_SLOTS = 2
MAX_CYCLE_SLOTS = 24
MIN_COMPLETED_CYCLES = 3
NEIGHBOR_CYCLES = 5


@dataclass(frozen=True, slots=True)
class ApplianceCycle:
    """One completed slot-aligned appliance event."""

    start_ms: int
    energy_kwh: tuple[float, ...]

    @property
    def total_kwh(self) -> float:
        return sum(self.energy_kwh)


@dataclass(frozen=True, slots=True)
class CyclicApplianceForecast:
    """Internal result carrying energy and empirical maturity."""

    energy_kwh: tuple[float, ...]
    estimated: bool = False


def forecast_cyclic_appliance(
    history: tuple[HistoricalFeatureSlot, ...],
    driver: LoadDriverSnapshot | None,
    component_key: str,
    slot_count: int,
) -> CyclicApplianceForecast:
    """Forecast only a currently observed event; never invent inactive P50 load."""
    if slot_count <= 0 or driver is None or not _driver_active(driver):
        return CyclicApplianceForecast((0.0,) * max(0, slot_count))

    cycles, active_prefix = appliance_cycles(history, component_key)
    if len(cycles) < MIN_COMPLETED_CYCLES:
        return CyclicApplianceForecast(
            _fallback_active_forecast(driver, slot_count), estimated=True
        )

    active_age_s = _driver_feature(driver, "cycle_active_age_s")
    progress = _driver_feature(driver, "cycle_progress_fraction")
    if not active_prefix and active_age_s is None and progress is None:
        # Power-only detection has no trustworthy event age until its first slot
        # finalizes. Do not restart a complete learned cycle on every replan.
        return CyclicApplianceForecast(
            _fallback_active_forecast(driver, slot_count), estimated=True
        )

    elapsed_slots = len(active_prefix)
    if active_age_s is not None:
        elapsed_slots = max(elapsed_slots, int(active_age_s // SLOT_S))
    median_length = round(statistics.median(len(c.energy_kwh) for c in cycles))
    if progress is not None and 0.0 < progress < 1.0:
        elapsed_slots = max(elapsed_slots, math.floor(progress * median_length))

    neighbors = _nearest_cycles(cycles, active_prefix)
    result = []
    for offset in range(slot_count):
        relative_slot = elapsed_slots + offset
        values = [
            cycle.energy_kwh[relative_slot]
            for cycle in neighbors
            if relative_slot < len(cycle.energy_kwh)
        ]
        result.append(float(statistics.median(values)) if values else 0.0)

    remaining_s = _driver_feature(driver, "cycle_remaining_s")
    if remaining_s is not None:
        keep_slots = max(1, math.ceil(remaining_s / SLOT_S))
        result = [
            value if index < keep_slots else 0.0 for index, value in enumerate(result)
        ]
    return CyclicApplianceForecast(tuple(max(0.0, value) for value in result))


def appliance_cycles(
    history: tuple[HistoricalFeatureSlot, ...], component_key: str
) -> tuple[tuple[ApplianceCycle, ...], tuple[float, ...]]:
    """Extract completed cycles and the unfinished tail from finalized history."""
    samples = []
    for item in history:
        component = next(
            (
                value
                for value in item.load_components
                if value.component_key == component_key
            ),
            None,
        )
        if (
            component is None
            or component.quality.coverage < 0.999
            or component.quality.flags
        ):
            samples.append((item.slot.start_ms, 0.0, False, False))
            continue
        active_feature = _component_feature(component, "cycle_active_fraction")
        active = (
            active_feature >= 0.05
            if active_feature is not None
            else component.energy_kwh >= ACTIVE_ENERGY_KWH
        )
        samples.append((item.slot.start_ms, component.energy_kwh, active, True))

    groups: list[list[tuple[int, float, bool, bool]]] = []
    current: list[tuple[int, float, bool, bool]] = []
    inactive_run = 0
    previous_start_ms: int | None = None
    for sample in samples:
        if previous_start_ms is not None and sample[0] != previous_start_ms + int(
            SLOT_S * 1000
        ):
            # A missing finalized slot makes both continuity and completion
            # unknowable. Discard the open fragment rather than joining events.
            current = []
            inactive_run = 0
        previous_start_ms = sample[0]
        if sample[2]:
            if not current:
                current = [sample]
            else:
                current.append(sample)
            inactive_run = 0
            continue
        if current:
            inactive_run += 1
            current.append(sample)
            if inactive_run <= MAX_INTERNAL_GAP_SLOTS:
                continue
            else:
                groups.append(current[:-inactive_run])
                current = []
                inactive_run = 0

    active_tail: tuple[float, ...] = ()
    if current:
        active_tail = tuple(value for _, value, _, _ in current)

    completed = []
    for group in groups:
        energy = tuple(max(0.0, value) for _, value, _, _ in group)
        if (
            energy
            and len(energy) <= MAX_CYCLE_SLOTS
            and sum(energy) >= 0.05
            and all(valid for _, _, _, valid in group)
        ):
            completed.append(ApplianceCycle(group[0][0], energy))
    return tuple(completed), active_tail


def _nearest_cycles(
    cycles: tuple[ApplianceCycle, ...], prefix: tuple[float, ...]
) -> tuple[ApplianceCycle, ...]:
    if not prefix:
        return cycles[-NEIGHBOR_CYCLES:]

    def distance(cycle: ApplianceCycle) -> float:
        compared = min(len(prefix), len(cycle.energy_kwh))
        if compared == 0:
            return math.inf
        scale = max(0.05, sum(prefix[:compared]))
        absolute_error = sum(
            abs(prefix[index] - cycle.energy_kwh[index]) for index in range(compared)
        )
        length_penalty = max(0, len(prefix) - len(cycle.energy_kwh))
        return absolute_error / scale + length_penalty

    return tuple(sorted(cycles, key=distance)[:NEIGHBOR_CYCLES])


def _fallback_active_forecast(
    driver: LoadDriverSnapshot, slot_count: int
) -> tuple[float, ...]:
    remaining_s = _driver_feature(driver, "cycle_remaining_s")
    keep_slots = (
        max(1, math.ceil(remaining_s / SLOT_S)) if remaining_s is not None else 1
    )
    slot_energy = max(0.0, driver.power_w) * SLOT_S / 3_600_000.0
    return tuple(
        slot_energy if index < keep_slots else 0.0 for index in range(slot_count)
    )


def _driver_active(driver: LoadDriverSnapshot) -> bool:
    active = _driver_feature(driver, "cycle_active_fraction")
    return active >= 0.5 if active is not None else driver.power_w >= ACTIVE_POWER_W


def _driver_feature(driver: LoadDriverSnapshot, key: str) -> float | None:
    return next(
        (feature.value for feature in driver.features if feature.feature_key == key),
        None,
    )


def _component_feature(component, key: str) -> float | None:
    return next(
        (feature.value for feature in component.features if feature.feature_key == key),
        None,
    )

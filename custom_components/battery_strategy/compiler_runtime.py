"""Active-slot execution state around the deterministic plan compiler."""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from .const import PV_CHARGING_ON
from .contracts import (
    BatteryPlan,
    LiveMeasurements,
    PlanCompilationState,
    PlanLiveDirective,
    SlotKey,
    SlotProgress,
)
from .contracts.common import SLOT_MS
from .models import StrategyOptions
from .plan_compiler import DeterministicPlanCompiler

if TYPE_CHECKING:
    from .compiler_runtime_store import CompilerRuntimeSnapshot

LOGGER = logging.getLogger(__name__)


class PlanCompilerRuntime:
    """Own active-slot commitment, measured progress and restart continuity."""

    def __init__(
        self,
        restored_snapshot: CompilerRuntimeSnapshot | None = None,
    ) -> None:
        self._compiler = DeterministicPlanCompiler()
        self._state = PlanCompilationState()
        self._restored_snapshot = restored_snapshot
        self._active_slot_id: str | None = None
        self._active_slot_end_ms = 0
        self._charged_kwh = 0.0
        self._discharged_kwh = 0.0
        self._next_slot_charged_kwh = 0.0
        self._next_slot_discharged_kwh = 0.0
        self._progress_reconstructable = True
        self._prorate_unrestored_discharge = False
        self._snapshot_dirty = False
        self._error: str | None = None
        self._last_accounting_at: dt.datetime | None = None
        self._last_battery_power_w: float | None = None

    def account(self, now: dt.datetime, battery_power_w: float) -> None:
        """Accumulate measured battery energy and remember the next sample."""
        if (
            self._last_accounting_at is not None
            and self._last_battery_power_w is not None
        ):
            elapsed_s = max(0.0, (now - self._last_accounting_at).total_seconds())
            if 0.0 < elapsed_s <= 15 * 60:
                interval_start_ms = int(self._last_accounting_at.timestamp() * 1000)
                interval_end_ms = int(now.timestamp() * 1000)
                active_end_ms = self._active_slot_end_ms
                active_s = 0.0
                next_s = 0.0
                if active_end_ms <= 0:
                    active_s = elapsed_s
                else:
                    active_s = max(
                        0.0,
                        (min(interval_end_ms, active_end_ms) - interval_start_ms)
                        / 1000.0,
                    )
                    next_s = max(
                        0.0,
                        (
                            min(interval_end_ms, active_end_ms + SLOT_MS)
                            - max(interval_start_ms, active_end_ms)
                        )
                        / 1000.0,
                    )
                self._add_energy(self._last_battery_power_w, active_s, next_slot=False)
                self._add_energy(self._last_battery_power_w, next_s, next_slot=True)
        self._last_accounting_at = now
        self._last_battery_power_w = float(battery_power_w)

    def suspend_accounting(self, now: dt.datetime) -> None:
        """Break integration continuity while battery feedback is unavailable."""
        self._last_accounting_at = now
        self._last_battery_power_w = None

    def sync_slot(
        self,
        slot_start_ms: int,
        now_ms: int,
        energy_totals: tuple[float | None, float | None],
    ) -> None:
        """Restore or reset progress exactly once at a slot boundary."""
        slot_id = str(max(0, int(slot_start_ms)))
        if slot_id == self._active_slot_id:
            return
        previous_slot_end_ms = self._active_slot_end_ms
        transitioned_while_running = self._active_slot_id is not None
        self._active_slot_id = slot_id
        self._active_slot_end_ms = int(slot_start_ms) + SLOT_MS
        carry_next_slot = (
            transitioned_while_running and int(slot_start_ms) == previous_slot_end_ms
        )
        self._charged_kwh = self._next_slot_charged_kwh if carry_next_slot else 0.0
        self._discharged_kwh = (
            self._next_slot_discharged_kwh if carry_next_slot else 0.0
        )
        self._next_slot_charged_kwh = 0.0
        self._next_slot_discharged_kwh = 0.0
        self._state = PlanCompilationState()
        self._progress_reconstructable = True
        self._prorate_unrestored_discharge = False

        restored = self._restored_snapshot
        self._restored_snapshot = None
        if (
            restored is not None
            and restored.compilation_state.slot is not None
            and restored.compilation_state.slot.start_ms == int(slot_start_ms)
            and now_ms < restored.compilation_state.slot.end_ms
        ):
            self._progress_reconstructable = self._restore_progress(
                restored, energy_totals
            )
            self._snapshot_dirty = True
            return

        if not transitioned_while_running and now_ms > int(slot_start_ms) + 5_000:
            # A deployment may start without a persisted runtime snapshot. Do
            # not reopen the full slot, but retain proportional discharge
            # permission for its unelapsed portion. Paid charging stays closed.
            self._prorate_unrestored_discharge = True

    def _add_energy(self, power_w: float, elapsed_s: float, *, next_slot: bool) -> None:
        """Attribute one measured power segment to its actual quarter-hour."""
        energy_kwh = float(power_w) * max(0.0, elapsed_s) / 3_600_000.0
        if energy_kwh < 0.0:
            if next_slot:
                self._next_slot_charged_kwh += abs(energy_kwh)
            else:
                self._charged_kwh += abs(energy_kwh)
        elif energy_kwh > 0.0:
            if next_slot:
                self._next_slot_discharged_kwh += energy_kwh
            else:
                self._discharged_kwh += energy_kwh

    def compile(
        self,
        plan: BatteryPlan | None,
        options: StrategyOptions,
        measurements: LiveMeasurements,
        now_ms: int,
        energy_totals: tuple[float | None, float | None] = (None, None),
    ) -> PlanLiveDirective:
        """Select and compile the current plan slot as one atomic operation."""
        if plan is None:
            slot_start_ms = int(now_ms) // SLOT_MS * SLOT_MS
            self.sync_slot(slot_start_ms, now_ms, energy_totals)
            self._state = PlanCompilationState()
            self._error = "no_plan"
            return self._closed_directive(options, now_ms=now_ms)
        plan_slot = next(
            (
                item
                for item in plan.slots
                if item.slot.start_ms <= now_ms < item.slot.end_ms
            ),
            None,
        )
        if plan_slot is None:
            slot_start_ms = int(now_ms) // SLOT_MS * SLOT_MS
            self.sync_slot(slot_start_ms, now_ms, energy_totals)
            self._state = PlanCompilationState()
            self._error = "no_current_plan_slot"
            return self._closed_directive(options, now_ms=now_ms)
        current_slot = plan_slot.slot
        self.sync_slot(current_slot.start_ms, now_ms, energy_totals)
        if not self._progress_reconstructable:
            self._error = "slot_progress_unrecoverable"
            return self._closed_directive(
                options,
                now_ms=now_ms,
                slot_start_ms=current_slot.start_ms,
                allow_pv_charge=True,
            )
        if self._prorate_unrestored_discharge and self._state.slot is None:
            remaining_fraction = max(
                0.0,
                min(
                    1.0,
                    (current_slot.end_ms - now_ms)
                    / (current_slot.end_ms - current_slot.start_ms),
                ),
            )
            self._state = PlanCompilationState(
                slot=current_slot,
                committed_plan_id=plan.plan_id,
                required_charge_commitment_kwh=0.0,
                discharge_budget_commitment_kwh=(
                    plan_slot.discharge_budget_kwh * remaining_fraction
                ),
                grid_charge_allowed=False,
            )
            self._prorate_unrestored_discharge = False
            self._snapshot_dirty = True
        try:
            compiled, next_state = self._compiler.compile(
                plan,
                SlotProgress(
                    slot=current_slot,
                    charged_kwh=max(0.0, self._charged_kwh),
                    discharged_kwh=max(0.0, self._discharged_kwh),
                    soc_pct=float(measurements.soc_pct),
                ),
                self._state,
                issued_at_ms=now_ms,
            )
            if next_state != self._state:
                self._snapshot_dirty = True
            self._state = next_state
            self._error = None
            return compiled
        except Exception as err:  # noqa: BLE001 - control must fail closed.
            self._state = PlanCompilationState()
            self._error = f"{type(err).__name__}: {err}"
            LOGGER.error("Plan compiler failed closed: %s", self._error)
            return self._closed_directive(
                options,
                now_ms=now_ms,
                slot_start_ms=current_slot.start_ms,
            )

    @staticmethod
    def _closed_directive(
        options: StrategyOptions,
        *,
        now_ms: int,
        slot_start_ms: int | None = None,
        allow_pv_charge: bool = False,
    ) -> PlanLiveDirective:
        """Return a valid directive with no commercial permission."""
        start_ms = (
            int(slot_start_ms)
            if slot_start_ms is not None
            else int(now_ms) // SLOT_MS * SLOT_MS
        )
        slot = SlotKey(start_ms=start_ms, end_ms=start_ms + SLOT_MS)
        pv_allowed = allow_pv_charge and options.pv_charging == PV_CHARGING_ON
        return PlanLiveDirective(
            directive_id=f"closed:{start_ms}:{now_ms}",
            plan_id="closed",
            issued_at_ms=now_ms,
            slot=slot,
            pv_charge_allowed=pv_allowed,
            grid_charge_allowed=False,
            required_charge_power_w=0.0,
            required_charge_remaining_kwh=0.0,
            max_pv_charge_power_w=(
                float(options.max_charge_power_w) if pv_allowed else 0.0
            ),
            max_grid_charge_power_w=0.0,
            max_discharge_power_w=float(options.max_discharge_power_w),
            discharge_budget_remaining_kwh=0.0,
            min_soc_pct=float(options.min_soc_pct),
            max_soc_pct=float(options.max_soc_pct),
        )

    def storage_snapshot(
        self,
        *,
        saved_at_ms: int,
        energy_totals: tuple[float | None, float | None],
        clean_shutdown: bool,
    ) -> CompilerRuntimeSnapshot | None:
        """Build the compact persistence payload for the active slot."""
        if self._state.slot is None:
            return None
        from .compiler_runtime_store import CompilerRuntimeSnapshot

        input_energy_kwh, output_energy_kwh = energy_totals
        return CompilerRuntimeSnapshot(
            saved_at_ms=saved_at_ms,
            compilation_state=self._state,
            charged_kwh=max(0.0, self._charged_kwh),
            discharged_kwh=max(0.0, self._discharged_kwh),
            input_energy_kwh=input_energy_kwh,
            output_energy_kwh=output_energy_kwh,
            clean_shutdown=clean_shutdown,
        )

    def mark_persisted(self) -> None:
        """Mark the current active-slot state as durably stored."""
        self._snapshot_dirty = False

    def _restore_progress(
        self,
        snapshot: CompilerRuntimeSnapshot,
        energy_totals: tuple[float | None, float | None],
    ) -> bool:
        charged_kwh = snapshot.charged_kwh
        discharged_kwh = snapshot.discharged_kwh
        current_input, current_output = energy_totals
        counters_exact = all(
            value is not None
            for value in (
                snapshot.input_energy_kwh,
                snapshot.output_energy_kwh,
                current_input,
                current_output,
            )
        )
        if counters_exact:
            assert snapshot.input_energy_kwh is not None
            assert snapshot.output_energy_kwh is not None
            assert current_input is not None
            assert current_output is not None
            if (
                current_input + 1e-6 < snapshot.input_energy_kwh
                or current_output + 1e-6 < snapshot.output_energy_kwh
            ):
                counters_exact = False
            else:
                charged_kwh += current_input - snapshot.input_energy_kwh
                discharged_kwh += current_output - snapshot.output_energy_kwh
        if not snapshot.clean_shutdown and not counters_exact:
            return False
        self._charged_kwh = max(0.0, charged_kwh)
        self._discharged_kwh = max(0.0, discharged_kwh)
        self._state = snapshot.compilation_state
        return True

    @property
    def snapshot_dirty(self) -> bool:
        return self._snapshot_dirty

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def progress_reconstructable(self) -> bool:
        return self._progress_reconstructable

    @property
    def charged_kwh(self) -> float:
        return self._charged_kwh

    @property
    def discharged_kwh(self) -> float:
        return self._discharged_kwh

    @property
    def compilation_state(self) -> PlanCompilationState:
        return self._state

    @property
    def active_slot_id(self) -> str | None:
        return self._active_slot_id

    @property
    def active_slot_end_ms(self) -> int:
        return self._active_slot_end_ms

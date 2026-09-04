"""Active-slot execution state around the deterministic plan compiler."""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from .contracts import PlanCompilationState, SlotProgress
from .contracts.common import SLOT_MS
from .models import StrategyInputs, StrategyOptions
from .plan_compiler import DeterministicPlanCompiler
from .plan_compiler_adapter import (
    closed_published_directive,
    contract_plan_from_strategy_plan,
    published_directive_from_contract,
)
from .plan_models import PlanLiveDirective, StrategyPlan

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
        self._progress_reconstructable = True
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
            elapsed_h = (
                max(0.0, (now - self._last_accounting_at).total_seconds()) / 3600.0
            )
            if 0.0 < elapsed_h <= 0.25:
                energy_kwh = self._last_battery_power_w * elapsed_h / 1000.0
                if energy_kwh < 0.0:
                    self._charged_kwh += abs(energy_kwh)
                elif energy_kwh > 0.0:
                    self._discharged_kwh += energy_kwh
        self._last_accounting_at = now
        self._last_battery_power_w = float(battery_power_w)

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
        transitioned_while_running = self._active_slot_id is not None
        self._active_slot_id = slot_id
        self._active_slot_end_ms = int(slot_start_ms) + SLOT_MS
        self._charged_kwh = 0.0
        self._discharged_kwh = 0.0
        self._state = PlanCompilationState()
        self._progress_reconstructable = True

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
            self._progress_reconstructable = False

    def compile(
        self,
        plan: StrategyPlan,
        options: StrategyOptions,
        inputs: StrategyInputs,
        now_ms: int,
    ) -> PlanLiveDirective:
        """Compile the directive consumed by the established live controller."""
        if not plan.points:
            self._state = PlanCompilationState()
            self._error = "no_plan"
            return closed_published_directive(options)
        if not self._progress_reconstructable:
            self._error = "slot_progress_unrecoverable"
            return closed_published_directive(
                options,
                slot_start_ms=int(plan.points[0].ts_ms),
                allow_pv_charge=True,
            )
        try:
            contract_plan = contract_plan_from_strategy_plan(plan, options, now_ms)
            current_slot = contract_plan.slots[0].slot
            compiled, next_state = self._compiler.compile(
                contract_plan,
                SlotProgress(
                    slot=current_slot,
                    charged_kwh=max(0.0, self._charged_kwh),
                    discharged_kwh=max(0.0, self._discharged_kwh),
                    soc_pct=float(inputs.soc_pct),
                ),
                self._state,
                issued_at_ms=now_ms,
            )
            if next_state != self._state:
                self._snapshot_dirty = True
            self._state = next_state
            self._error = None
            return published_directive_from_contract(compiled, plan, options)
        except Exception as err:  # noqa: BLE001 - control must fail closed.
            self._state = PlanCompilationState()
            self._error = f"{type(err).__name__}: {err}"
            LOGGER.error("Plan compiler failed closed: %s", self._error)
            return closed_published_directive(
                options,
                slot_start_ms=int(plan.points[0].ts_ms),
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

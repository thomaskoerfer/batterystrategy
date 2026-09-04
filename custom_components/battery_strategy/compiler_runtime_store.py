"""Versioned Home Assistant persistence for active compiler progress."""

from __future__ import annotations

import math
from dataclasses import dataclass

from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .contracts import PlanCompilationState, SlotKey

STORE_VERSION = 1


@dataclass(frozen=True, slots=True)
class CompilerRuntimeSnapshot:
    """Minimal state needed to continue one active slot without reopening it."""

    saved_at_ms: int
    compilation_state: PlanCompilationState
    charged_kwh: float
    discharged_kwh: float
    input_energy_kwh: float | None
    output_energy_kwh: float | None
    clean_shutdown: bool

    def __post_init__(self) -> None:
        if self.saved_at_ms < 0 or self.compilation_state.slot is None:
            raise ValueError("active compiler snapshot requires timestamp and slot")
        if any(
            not math.isfinite(value) or value < 0.0
            for value in (self.charged_kwh, self.discharged_kwh)
        ):
            raise ValueError("compiler progress must be non-negative")
        for value in (self.input_energy_kwh, self.output_energy_kwh):
            if value is not None and (not math.isfinite(value) or value < 0.0):
                raise ValueError("battery energy counters must be non-negative")

    def as_storage_dict(self) -> dict[str, object]:
        """Serialize without exposing Home Assistant or vendor state downstream."""
        state = self.compilation_state
        slot = state.slot
        assert slot is not None
        return {
            "saved_at_ms": self.saved_at_ms,
            "slot_start_ms": slot.start_ms,
            "slot_end_ms": slot.end_ms,
            "committed_plan_id": state.committed_plan_id,
            "required_charge_commitment_kwh": state.required_charge_commitment_kwh,
            "discharge_budget_commitment_kwh": state.discharge_budget_commitment_kwh,
            "grid_charge_allowed": state.grid_charge_allowed,
            "charged_kwh": self.charged_kwh,
            "discharged_kwh": self.discharged_kwh,
            "input_energy_kwh": self.input_energy_kwh,
            "output_energy_kwh": self.output_energy_kwh,
            "clean_shutdown": self.clean_shutdown,
        }

    @classmethod
    def from_storage_dict(cls, raw: object) -> CompilerRuntimeSnapshot | None:
        """Validate persisted data and reject incomplete progress safely."""
        if not isinstance(raw, dict):
            return None
        try:
            committed_plan_id = raw["committed_plan_id"]
            grid_charge_allowed = raw["grid_charge_allowed"]
            clean_shutdown = raw.get("clean_shutdown", False)
            if not isinstance(committed_plan_id, str) or not committed_plan_id:
                return None
            if not isinstance(grid_charge_allowed, bool) or not isinstance(
                clean_shutdown, bool
            ):
                return None
            slot = SlotKey(int(raw["slot_start_ms"]), int(raw["slot_end_ms"]))
            state = PlanCompilationState(
                slot=slot,
                committed_plan_id=committed_plan_id,
                required_charge_commitment_kwh=float(
                    raw["required_charge_commitment_kwh"]
                ),
                discharge_budget_commitment_kwh=float(
                    raw["discharge_budget_commitment_kwh"]
                ),
                grid_charge_allowed=grid_charge_allowed,
            )
            return cls(
                saved_at_ms=int(raw["saved_at_ms"]),
                compilation_state=state,
                charged_kwh=float(raw["charged_kwh"]),
                discharged_kwh=float(raw["discharged_kwh"]),
                input_energy_kwh=_optional_float(raw.get("input_energy_kwh")),
                output_energy_kwh=_optional_float(raw.get("output_energy_kwh")),
                clean_shutdown=clean_shutdown,
            )
        except (KeyError, TypeError, ValueError):
            return None


class CompilerRuntimeStore:
    """Home Assistant storage adapter for one config entry's active slot."""

    def __init__(self, hass, entry_id: str) -> None:
        self._store: Store[dict[str, object]] = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}.compiler_runtime.{entry_id}",
            atomic_writes=True,
        )

    async def load(self) -> CompilerRuntimeSnapshot | None:
        """Load and validate the last active-slot snapshot."""
        return CompilerRuntimeSnapshot.from_storage_dict(await self._store.async_load())

    async def save(self, snapshot: CompilerRuntimeSnapshot) -> None:
        """Persist one compact active-slot snapshot atomically."""
        await self._store.async_save(snapshot.as_storage_dict())


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)

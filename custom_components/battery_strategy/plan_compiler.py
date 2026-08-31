"""Side-effect-free compilation of optimizer intent for live control."""

from __future__ import annotations

from .contracts import BatteryPlan, PlanLiveDirective, SlotProgress

ENERGY_EPSILON_KWH = 1e-9


class DeterministicPlanCompiler:
    """Compile one current plan slot without reinterpreting economics."""

    def compile(
        self,
        plan: BatteryPlan,
        progress: SlotProgress,
        issued_at_ms: int,
    ) -> PlanLiveDirective:
        """Return the complete live permission for ``progress.slot``."""
        if issued_at_ms < 0:
            raise ValueError("issued_at_ms must be non-negative")

        try:
            plan_slot = next(item for item in plan.slots if item.slot == progress.slot)
        except StopIteration as err:
            raise ValueError("progress slot is not present in the battery plan") from err

        required_remaining_kwh = max(
            0.0,
            plan_slot.required_charge_kwh - progress.charged_kwh,
        )
        budget_remaining_kwh = max(
            0.0,
            plan_slot.discharge_budget_kwh - progress.discharged_kwh,
        )
        available_above_min_kwh = (
            plan.constraints.capacity_kwh
            * max(0.0, progress.soc_pct - plan.constraints.min_soc_pct)
            / 100.0
        )
        budget_remaining_kwh = min(
            budget_remaining_kwh,
            available_above_min_kwh,
        )

        has_grid_commitment = (
            plan_slot.grid_charge_allowed
            and plan_slot.planned_grid_charge_kwh > ENERGY_EPSILON_KWH
            and plan_slot.required_charge_kwh > ENERGY_EPSILON_KWH
        )
        pv_charge_allowed = bool(plan_slot.pv_charge_allowed)
        return PlanLiveDirective(
            directive_id=(
                f"{plan.plan_id}:{progress.slot.start_ms}:{issued_at_ms}"
            ),
            plan_id=plan.plan_id,
            issued_at_ms=issued_at_ms,
            slot=progress.slot,
            pv_charge_allowed=pv_charge_allowed,
            grid_charge_allowed=has_grid_commitment,
            required_charge_remaining_kwh=required_remaining_kwh,
            max_pv_charge_power_w=(
                plan.constraints.max_charge_power_w if pv_charge_allowed else 0.0
            ),
            max_grid_charge_power_w=(
                plan.constraints.max_charge_power_w if has_grid_commitment else 0.0
            ),
            # This is a physical cap, not commercial permission. Price-sensitive
            # discharge remains blocked by a zero remaining budget; an explicit
            # live load/manual policy may use the same physical limit.
            max_discharge_power_w=plan.constraints.max_discharge_power_w,
            discharge_budget_remaining_kwh=budget_remaining_kwh,
            min_soc_pct=plan.constraints.min_soc_pct,
            max_soc_pct=plan.constraints.max_soc_pct,
        )

"""Side-effect-free compilation of optimizer intent for live control."""

from __future__ import annotations

from .contracts import (
    BatteryPlan,
    DischargeCommitmentPhase,
    DischargeReconciliation,
    PlanCompilationState,
    PlanLiveDirective,
    SlotProgress,
)

ENERGY_EPSILON_KWH = 1e-9


class DeterministicPlanCompiler:
    """Compile one current plan slot without reinterpreting economics."""

    def compile(
        self,
        plan: BatteryPlan,
        progress: SlotProgress,
        state: PlanCompilationState,
        issued_at_ms: int,
        *,
        discharge_reconciliation: DischargeReconciliation = (
            DischargeReconciliation.FINALIZE_CONSERVATIVELY
        ),
    ) -> tuple[PlanLiveDirective, PlanCompilationState]:
        """Return the complete live permission for ``progress.slot``."""
        if issued_at_ms < 0:
            raise ValueError("issued_at_ms must be non-negative")
        if plan.generated_at_ms > issued_at_ms:
            raise ValueError("plan cannot be generated after the directive")

        try:
            plan_slot = next(item for item in plan.slots if item.slot == progress.slot)
        except StopIteration as err:
            raise ValueError(
                "progress slot is not present in the battery plan"
            ) from err

        has_grid_commitment = (
            plan_slot.grid_charge_allowed
            and plan_slot.planned_grid_charge_kwh > ENERGY_EPSILON_KWH
            and plan_slot.required_charge_kwh > ENERGY_EPSILON_KWH
        )
        if state.slot != progress.slot:
            next_state = PlanCompilationState(
                slot=progress.slot,
                committed_plan_id=plan.plan_id,
                required_charge_commitment_kwh=(
                    plan_slot.required_charge_kwh if has_grid_commitment else 0.0
                ),
                discharge_budget_commitment_kwh=plan_slot.discharge_budget_kwh,
                discharge_commitment_phase=(
                    DischargeCommitmentPhase.PROVISIONAL
                    if plan.generated_at_ms < progress.slot.start_ms
                    else DischargeCommitmentPhase.FINAL
                ),
                grid_charge_allowed=has_grid_commitment,
            )
        else:
            first_post_boundary_plan = (
                state.discharge_commitment_phase is DischargeCommitmentPhase.PROVISIONAL
                and plan.generated_at_ms >= progress.slot.start_ms
            )
            reconcile_discharge = (
                first_post_boundary_plan
                and discharge_reconciliation is DischargeReconciliation.RECONCILE
            )
            finalize_discharge = (
                first_post_boundary_plan
                and discharge_reconciliation is not DischargeReconciliation.WAIT
            )
            next_required = min(
                state.required_charge_commitment_kwh,
                plan_slot.required_charge_kwh if has_grid_commitment else 0.0,
            )
            next_state = PlanCompilationState(
                slot=state.slot,
                committed_plan_id=(
                    plan.plan_id if finalize_discharge else state.committed_plan_id
                ),
                required_charge_commitment_kwh=next_required,
                discharge_budget_commitment_kwh=(
                    plan_slot.discharge_budget_kwh
                    if reconcile_discharge
                    else state.discharge_budget_commitment_kwh
                    if first_post_boundary_plan
                    and discharge_reconciliation is DischargeReconciliation.WAIT
                    else min(
                        state.discharge_budget_commitment_kwh,
                        plan_slot.discharge_budget_kwh,
                    )
                ),
                discharge_commitment_phase=(
                    DischargeCommitmentPhase.FINAL
                    if finalize_discharge
                    else state.discharge_commitment_phase
                ),
                grid_charge_allowed=(
                    state.grid_charge_allowed
                    and has_grid_commitment
                    and next_required > ENERGY_EPSILON_KWH
                ),
            )

        required_remaining_kwh = max(
            0.0,
            next_state.required_charge_commitment_kwh - progress.charged_kwh,
        )
        budget_remaining_kwh = max(
            0.0,
            next_state.discharge_budget_commitment_kwh - progress.discharged_kwh,
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

        pv_charge_allowed = bool(plan_slot.pv_charge_allowed)
        directive = PlanLiveDirective(
            directive_id=(
                f"{next_state.committed_plan_id}:{progress.slot.start_ms}:"
                f"{issued_at_ms}"
            ),
            plan_id=str(next_state.committed_plan_id),
            issued_at_ms=issued_at_ms,
            slot=progress.slot,
            pv_charge_allowed=pv_charge_allowed,
            grid_charge_allowed=next_state.grid_charge_allowed,
            required_charge_power_w=(
                min(
                    plan.constraints.max_charge_power_w,
                    next_state.required_charge_commitment_kwh / 0.25 * 1000.0,
                )
                if next_state.grid_charge_allowed
                else 0.0
            ),
            required_charge_remaining_kwh=required_remaining_kwh,
            max_pv_charge_power_w=(
                plan.constraints.max_charge_power_w if pv_charge_allowed else 0.0
            ),
            max_grid_charge_power_w=(
                plan.constraints.max_charge_power_w
                if next_state.grid_charge_allowed
                else 0.0
            ),
            # This is a physical cap, not commercial permission. Price-sensitive
            # discharge remains blocked by a zero remaining budget; an explicit
            # live load/manual policy may use the same physical limit.
            max_discharge_power_w=plan.constraints.max_discharge_power_w,
            discharge_budget_remaining_kwh=budget_remaining_kwh,
            min_soc_pct=plan.constraints.min_soc_pct,
            max_soc_pct=plan.constraints.max_soc_pct,
        )
        return directive, next_state

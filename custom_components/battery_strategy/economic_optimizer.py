"""Side-effect-free battery economic optimizer."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    BatteryPlan,
    BatteryPlanSlot,
    OptimizationProblem,
    PlanMode,
)

SLOT_H = 0.25
ENERGY_STEP_KWH = 0.025
ECONOMIC_COST_TIE_EUR = 1e-9
PV_RECOVERY_LOOKAHEAD_H = 18.0
SCARCE_VALUE_TIE_CT = 0.5
OPTIMIZER_VERSION = "economic-dp-v2"


@dataclass(slots=True)
class _Action:
    """Mutable canonical action used only inside one optimization call."""

    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0
    soc_start_kwh: float = 0.0
    soc_end_kwh: float = 0.0
    pv_charge_kwh: float = 0.0
    grid_charge_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0


class DynamicProgrammingOptimizer:
    """Optimize one immutable problem without I/O or hidden runtime context."""

    def optimize(self, problem: OptimizationProblem) -> BatteryPlan:
        """Return the deterministic economic plan for ``problem``."""
        load_slots = problem.forecast.load.slots
        pv_slots = problem.forecast.pv.slots
        if tuple(item.slot for item in load_slots) != tuple(
            item.slot for item in pv_slots
        ):
            raise ValueError("load and PV forecasts must use the same slot grid")
        if not load_slots:
            return BatteryPlan(
                f"{problem.problem_id}:{OPTIMIZER_VERSION}",
                problem.problem_id,
                problem.as_of_ms,
                OPTIMIZER_VERSION,
                problem.constraints,
                (),
                0.0,
                0.0,
            )

        load_kwh = tuple(max(0.0, item.energy.p50_kwh) for item in load_slots)
        pv_kwh = tuple(max(0.0, item.energy.p50_kwh) for item in pv_slots)
        prices = tuple(float(item.import_price_ct_per_kwh) for item in problem.market)
        export_prices = tuple(
            max(
                float(item.export_price_ct_per_kwh),
                float(problem.policy.export_opportunity_ct_per_kwh),
            )
            for item in problem.market
        )
        net_load = tuple(max(0.0, load - pv) for load, pv in zip(load_kwh, pv_kwh))
        surplus = tuple(max(0.0, pv - load) for load, pv in zip(load_kwh, pv_kwh))
        actions = self._dynamic_program(
            problem, prices, export_prices, net_load, surplus
        )
        self._canonicalize(problem, prices, net_load, surplus, actions)
        budgets = self._discharge_budgets(
            problem, prices, export_prices, net_load, surplus, actions
        )

        capacity = problem.constraints.capacity_kwh
        plan_slots = []
        for index, action in enumerate(actions):
            mode = PlanMode.IDLE
            if action.charge_kwh > 1e-9:
                mode = PlanMode.CHARGE
            elif action.discharge_kwh > 1e-9:
                mode = PlanMode.DISCHARGE
            required = action.charge_kwh if action.grid_charge_kwh > 1e-9 else 0.0
            plan_slots.append(
                BatteryPlanSlot(
                    slot=load_slots[index].slot,
                    mode=mode,
                    pv_charge_allowed=problem.policy.pv_charging_allowed,
                    grid_charge_allowed=problem.policy.grid_charging_allowed,
                    planned_charge_kwh=action.charge_kwh,
                    planned_discharge_kwh=action.discharge_kwh,
                    required_charge_kwh=required,
                    discharge_budget_kwh=max(action.discharge_kwh, budgets[index]),
                    expected_soc_start_pct=100.0 * action.soc_start_kwh / capacity,
                    expected_soc_end_pct=100.0 * action.soc_end_kwh / capacity,
                    planned_pv_charge_kwh=action.pv_charge_kwh,
                    planned_grid_charge_kwh=action.grid_charge_kwh,
                )
            )

        baseline = sum(
            (net * price - pv_surplus * export_price) / 100.0
            for net, pv_surplus, price, export_price in zip(
                net_load, surplus, prices, export_prices
            )
        )
        optimized = sum(
            (
                action.grid_import_kwh * prices[index]
                - action.grid_export_kwh * export_prices[index]
            )
            / 100.0
            for index, action in enumerate(actions)
        )
        return BatteryPlan(
            plan_id=f"{problem.problem_id}:{OPTIMIZER_VERSION}",
            problem_id=problem.problem_id,
            generated_at_ms=problem.as_of_ms,
            optimizer_version=OPTIMIZER_VERSION,
            constraints=problem.constraints,
            slots=tuple(plan_slots),
            baseline_cost_eur=baseline,
            optimized_cost_eur=optimized,
        )

    def _dynamic_program(
        self, problem, prices, export_prices, net_load, surplus
    ) -> list[_Action]:
        constraints = problem.constraints
        policy = problem.policy
        eta_c = math.sqrt(constraints.round_trip_efficiency)
        eta_d = eta_c
        min_energy = constraints.capacity_kwh * constraints.min_soc_pct / 100.0
        max_energy = constraints.capacity_kwh * constraints.max_soc_pct / 100.0
        start_energy = _clamp(
            constraints.capacity_kwh * problem.battery.soc_pct / 100.0,
            min_energy,
            max_energy,
        )
        max_charge_slot = constraints.max_charge_power_w / 1000.0 * SLOT_H
        max_discharge_slot = constraints.max_discharge_power_w / 1000.0 * SLOT_H
        step = min(ENERGY_STEP_KWH, max_energy - min_energy)
        state_count = round((max_energy - min_energy) / step) + 1
        energies = [min_energy + index * step for index in range(state_count)]

        def state_index(energy):
            value = round((_clamp(energy, min_energy, max_energy) - min_energy) / step)
            return max(0, min(state_count - 1, value))

        slot_count = len(prices)
        inf = 10**18
        # Mode remains a path dimension for exact deterministic reproducibility even
        # though economic switching cost is currently zero.
        modes = (-1, 0, 1)
        mode_index = {-1: 0, 0: 1, 1: 2}
        costs = [[[inf] * 3 for _ in energies] for _ in range(slot_count + 1)]
        grid_charge = [[[inf] * 3 for _ in energies] for _ in range(slot_count + 1)]
        timing = [[[-inf] * 3 for _ in energies] for _ in range(slot_count + 1)]
        previous = [[[None] * 3 for _ in energies] for _ in range(slot_count + 1)]
        start_index = state_index(start_energy)
        costs[0][start_index][mode_index[0]] = 0.0
        grid_charge[0][start_index][mode_index[0]] = 0.0
        timing[0][start_index][mode_index[0]] = 0.0

        future_peak = [0.0] * (slot_count + 1)
        for index in range(slot_count - 1, -1, -1):
            future_peak[index] = max(future_peak[index + 1], prices[index])

        for slot_index in range(slot_count):
            discharge_limit = (
                min(net_load[slot_index], max_discharge_slot)
                if policy.discharge_allowed
                else 0.0
            )
            for energy_index, energy_now in enumerate(energies):
                minimum_next = max(
                    min_energy,
                    energy_now
                    - min(max_discharge_slot / eta_d, discharge_limit / eta_d),
                )
                maximum_next = min(max_energy, energy_now + eta_c * max_charge_slot)
                for previous_mode_index, _previous_mode in enumerate(modes):
                    base_cost = costs[slot_index][energy_index][previous_mode_index]
                    if base_cost >= inf:
                        continue
                    for next_index in range(
                        state_index(minimum_next), state_index(maximum_next) + 1
                    ):
                        energy_next = energies[next_index]
                        delta = energy_next - energy_now
                        charge_in = max(0.0, delta / eta_c)
                        discharge_out = max(0.0, -delta * eta_d)
                        if charge_in > max_charge_slot + 1e-9:
                            continue
                        if discharge_out > discharge_limit + 1e-9:
                            continue
                        if charge_in > 1e-9:
                            if not (
                                policy.pv_charging_allowed
                                or policy.grid_charging_allowed
                            ):
                                continue
                            if (
                                not policy.grid_charging_allowed
                                and charge_in > surplus[slot_index] + 1e-9
                            ):
                                continue
                            if (
                                not policy.pv_charging_allowed
                                and surplus[slot_index] > 1e-9
                            ):
                                continue
                        if (
                            discharge_out > 1e-9
                            and policy.discharge_floor_ct_per_kwh is not None
                            and prices[slot_index]
                            < policy.discharge_floor_ct_per_kwh - 1e-9
                        ):
                            continue

                        grid_input = max(0.0, charge_in - surplus[slot_index])
                        if grid_input > 1e-9:
                            if future_peak[
                                slot_index + 1
                            ] * constraints.round_trip_efficiency < (
                                prices[slot_index] + policy.min_margin_ct_per_kwh
                            ):
                                continue
                        imported = (
                            max(0.0, net_load[slot_index] - discharge_out) + grid_input
                        )
                        exported = max(0.0, surplus[slot_index] - charge_in)
                        step_cost = (
                            imported * prices[slot_index]
                            - exported * export_prices[slot_index]
                            + discharge_out * policy.min_margin_ct_per_kwh
                        ) / 100.0
                        current_mode = (
                            1
                            if charge_in > 1e-4
                            else (-1 if discharge_out > 1e-4 else 0)
                        )
                        current_mode_index = mode_index[current_mode]
                        candidate_cost = base_cost + step_cost
                        candidate_grid = (
                            grid_charge[slot_index][energy_index][previous_mode_index]
                            + grid_input
                        )
                        candidate_timing = (
                            timing[slot_index][energy_index][previous_mode_index]
                            + grid_input * slot_index
                        )
                        if _path_is_better(
                            candidate_cost,
                            candidate_grid,
                            candidate_timing,
                            costs[slot_index + 1][next_index][current_mode_index],
                            grid_charge[slot_index + 1][next_index][current_mode_index],
                            timing[slot_index + 1][next_index][current_mode_index],
                        ):
                            costs[slot_index + 1][next_index][current_mode_index] = (
                                candidate_cost
                            )
                            grid_charge[slot_index + 1][next_index][
                                current_mode_index
                            ] = candidate_grid
                            timing[slot_index + 1][next_index][current_mode_index] = (
                                candidate_timing
                            )
                            previous[slot_index + 1][next_index][current_mode_index] = (
                                energy_index,
                                previous_mode_index,
                                charge_in,
                                discharge_out,
                            )

        best = None
        for energy_index, energy in enumerate(energies):
            terminal_credit = (
                policy.terminal_value_ct_per_kwh * max(0.0, energy - min_energy) / 100.0
            )
            for current_mode_index in range(3):
                candidate = (
                    costs[slot_count][energy_index][current_mode_index]
                    - terminal_credit,
                    grid_charge[slot_count][energy_index][current_mode_index],
                    timing[slot_count][energy_index][current_mode_index],
                    energy_index,
                    current_mode_index,
                )
                if best is None or _path_is_better(
                    candidate[0], candidate[1], candidate[2], best[0], best[1], best[2]
                ):
                    best = candidate
        assert best is not None

        actions = [_Action() for _ in prices]
        energy_index = best[3]
        mode_cursor = best[4]
        for slot_index in range(slot_count, 0, -1):
            record = previous[slot_index][energy_index][mode_cursor]
            if record is None:
                record = (energy_index, mode_index[0], 0.0, 0.0)
            previous_energy_index, previous_mode_index, charge_in, discharge_out = (
                record
            )
            action = actions[slot_index - 1]
            action.charge_kwh = max(0.0, charge_in)
            action.discharge_kwh = max(0.0, discharge_out)
            action.soc_start_kwh = energies[previous_energy_index]
            action.soc_end_kwh = energies[energy_index]
            energy_index = previous_energy_index
            mode_cursor = previous_mode_index
        return actions

    def _canonicalize(self, problem, prices, net_load, surplus, actions):
        policy = problem.policy
        constraints = problem.constraints
        eta_c = math.sqrt(constraints.round_trip_efficiency)
        eta_d = eta_c
        min_energy = constraints.capacity_kwh * constraints.min_soc_pct / 100.0
        max_energy = constraints.capacity_kwh * constraints.max_soc_pct / 100.0
        max_charge = constraints.max_charge_power_w / 1000.0 * SLOT_H
        max_discharge = constraints.max_discharge_power_w / 1000.0 * SLOT_H

        quantum = ENERGY_STEP_KWH / eta_c
        if policy.grid_charging_allowed:
            for source_index, source in enumerate(actions):
                source_grid = max(0.0, source.charge_kwh - surplus[source_index])
                if not (
                    surplus[source_index] > 1e-9 and 1e-9 < source_grid < quantum - 1e-9
                ):
                    continue
                deadline = next(
                    (
                        index
                        for index in range(source_index + 1, len(actions))
                        if actions[index].discharge_kwh > 1e-9
                    ),
                    len(actions),
                )
                candidates = []
                for target_index in range(source_index + 1, deadline):
                    target_grid = max(
                        0.0, actions[target_index].charge_kwh - surplus[target_index]
                    )
                    if (
                        target_grid < quantum - 1e-9
                        or prices[target_index] >= prices[source_index] - 1e-9
                    ):
                        continue
                    capacity = max(0.0, max_charge - actions[target_index].charge_kwh)
                    if capacity > 1e-9:
                        candidates.append(
                            (
                                prices[target_index],
                                -target_index,
                                target_index,
                                capacity,
                            )
                        )
                if sum(item[3] for item in candidates) + 1e-9 < source_grid:
                    continue
                source.charge_kwh = surplus[source_index]
                remaining = source_grid
                for _price, _order, target_index, capacity in sorted(candidates):
                    moved = min(remaining, capacity)
                    actions[target_index].charge_kwh += moved
                    remaining -= moved
                    if remaining <= 1e-9:
                        break

        energy = _clamp(
            constraints.capacity_kwh * problem.battery.soc_pct / 100.0,
            min_energy,
            max_energy,
        )
        for index, action in enumerate(actions):
            action.soc_start_kwh = energy
            charge = min(
                action.charge_kwh,
                max_charge,
                max(0.0, (max_energy - energy) / eta_c),
            )
            if not policy.grid_charging_allowed:
                charge = min(
                    charge, surplus[index] if policy.pv_charging_allowed else 0.0
                )
            elif not policy.pv_charging_allowed and surplus[index] > 1e-9:
                charge = 0.0
            discharge = min(
                action.discharge_kwh,
                max_discharge,
                net_load[index],
                max(0.0, (energy - min_energy) * eta_d),
            )
            if not policy.discharge_allowed:
                discharge = 0.0
            action.charge_kwh = charge
            action.discharge_kwh = discharge
            action.grid_charge_kwh = max(0.0, charge - surplus[index])
            action.pv_charge_kwh = charge - action.grid_charge_kwh
            action.grid_import_kwh = (
                max(0.0, net_load[index] - discharge) + action.grid_charge_kwh
            )
            action.grid_export_kwh = max(0.0, surplus[index] - charge)
            energy = _clamp(
                energy + charge * eta_c - discharge / eta_d, min_energy, max_energy
            )
            action.soc_end_kwh = energy

    def _discharge_budgets(
        self, problem, prices, export_prices, net_load, surplus, actions
    ) -> list[float]:
        constraints = problem.constraints
        policy = problem.policy
        eta_c = math.sqrt(constraints.round_trip_efficiency)
        eta_d = eta_c
        min_energy = constraints.capacity_kwh * constraints.min_soc_pct / 100.0
        max_energy = constraints.capacity_kwh * constraints.max_soc_pct / 100.0
        max_slot = constraints.max_discharge_power_w / 1000.0 * SLOT_H
        lookahead = max(1, round(PV_RECOVERY_LOOKAHEAD_H / SLOT_H))
        budgets = []

        def replacement_is_economic(current_price, replacement_price):
            return current_price + 1e-9 >= (
                replacement_price / constraints.round_trip_efficiency
                + policy.min_margin_ct_per_kwh
            )

        for index, action in enumerate(actions):
            if action.grid_charge_kwh > 1e-6 or not policy.discharge_allowed:
                budgets.append(0.0)
                continue
            available = max(0.0, (action.soc_start_kwh - min_energy) * eta_d)
            maximum = min(max_slot, available)
            if maximum <= 1e-6 or prices[index] < (
                export_prices[index] + policy.min_margin_ct_per_kwh
            ):
                budgets.append(max(0.0, action.discharge_kwh))
                continue

            end = min(len(actions), index + 1 + lookahead)
            for later in range(index + 1, end):
                if prices[later] > prices[index] + SCARCE_VALUE_TIE_CT:
                    end = later
                    break
            future_surplus = sum(surplus[index + 1 : end])
            recoverable = future_surplus * eta_c * policy.pv_recovery_confidence
            headroom = max(0.0, max_energy - action.soc_end_kwh)
            safe_recovery = (
                max(
                    0.0,
                    recoverable - headroom - policy.pv_recovery_reserve_kwh,
                )
                * eta_d
            )
            pv_budget = min(
                maximum,
                max(
                    0.0,
                    safe_recovery
                    - action.charge_kwh * constraints.round_trip_efficiency,
                ),
            )

            replacement = 0.0
            reserved = 0.0
            for later in range(index + 1, len(actions)):
                pv_input = min(actions[later].charge_kwh, surplus[later])
                grid_input = max(0.0, actions[later].charge_kwh - pv_input)
                if replacement_is_economic(prices[index], prices[later]):
                    replacement += grid_input * constraints.round_trip_efficiency
                if replacement_is_economic(prices[index], export_prices[later]):
                    replacement += (
                        pv_input
                        * policy.pv_recovery_confidence
                        * constraints.round_trip_efficiency
                    )
                if prices[later] <= prices[index] + 1e-9:
                    continue
                future_need = min(max_slot, net_load[later])
                used = min(replacement, future_need)
                replacement -= used
                reserved += future_need - used

            scarce = 0.0
            floor = policy.discharge_floor_ct_per_kwh or 0.0
            if action.charge_kwh <= 1e-6 and prices[index] >= floor:
                scarce = max(0.0, available + safe_recovery - reserved)
            budgets.append(
                min(
                    maximum,
                    max(action.discharge_kwh, pv_budget, min(maximum, scarce)),
                )
            )
        return budgets


def _path_is_better(
    candidate_cost,
    candidate_grid,
    candidate_timing,
    best_cost,
    best_grid,
    best_timing,
):
    delta = candidate_cost - best_cost
    if delta < -ECONOMIC_COST_TIE_EUR:
        return True
    if abs(delta) > ECONOMIC_COST_TIE_EUR:
        return False
    grid_delta = candidate_grid - best_grid
    if grid_delta < -1e-9:
        return True
    if abs(grid_delta) > 1e-9:
        return False
    return candidate_timing > best_timing + 1e-9


def _clamp(value, low, high):
    return max(low, min(high, float(value)))

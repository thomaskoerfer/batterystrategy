"""Side-effect-free battery economic optimizer."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace

from .contracts import (
    BatteryPlan,
    BatteryPlanSlot,
    OptimizationProblem,
    PlanMode,
)

SLOT_H = 0.25
ENERGY_STEP_KWH = 0.025
STOCHASTIC_RECOURSE_STEP_KWH = 0.1
STOCHASTIC_MAX_RECOURSE_STATES = 600
ECONOMIC_COST_TIE_EUR = 1e-9
PV_RECOVERY_LOOKAHEAD_H = 18.0
SCARCE_VALUE_TIE_CT = 0.5
OPTIMIZER_VERSION = "economic-dp-v2"
STOCHASTIC_OPTIMIZER_VERSION = "stochastic-two-stage-dp-v1"


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
        return self._build_plan(
            problem, prices, export_prices, net_load, surplus, net_load
        )

    def _build_plan(
        self,
        problem,
        prices,
        export_prices,
        net_load,
        surplus,
        discharge_limit,
        *,
        required_first_end_kwh=None,
        optimizer_version=OPTIMIZER_VERSION,
        first_budget_is_action=False,
    ):
        load_slots = problem.forecast.load.slots
        actions = self._dynamic_program(
            problem,
            prices,
            export_prices,
            net_load,
            surplus,
            discharge_limit,
            required_first_end_kwh=required_first_end_kwh,
        )
        self._canonicalize(
            problem,
            prices,
            net_load,
            surplus,
            discharge_limit,
            actions,
            fixed_first_transition=required_first_end_kwh is not None,
        )
        budgets = self._discharge_budgets(
            problem, prices, export_prices, discharge_limit, surplus, actions
        )
        if first_budget_is_action and budgets:
            budgets[0] = actions[0].discharge_kwh

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
            plan_id=f"{problem.problem_id}:{optimizer_version}",
            problem_id=problem.problem_id,
            generated_at_ms=problem.as_of_ms,
            optimizer_version=optimizer_version,
            constraints=problem.constraints,
            slots=tuple(plan_slots),
            baseline_cost_eur=baseline,
            optimized_cost_eur=optimized,
        )

    def _dynamic_program(
        self,
        problem,
        prices,
        export_prices,
        net_load,
        surplus,
        discharge_limit,
        *,
        required_first_end_kwh=None,
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
            slot_discharge_limit = (
                min(discharge_limit[slot_index], max_discharge_slot)
                if policy.discharge_allowed
                else 0.0
            )
            for energy_index, energy_now in enumerate(energies):
                minimum_next = max(
                    min_energy,
                    energy_now
                    - min(max_discharge_slot / eta_d, slot_discharge_limit / eta_d),
                )
                maximum_next = min(max_energy, energy_now + eta_c * max_charge_slot)
                for previous_mode_index, _previous_mode in enumerate(modes):
                    base_cost = costs[slot_index][energy_index][previous_mode_index]
                    if base_cost >= inf:
                        continue
                    next_indexes = range(
                        state_index(minimum_next), state_index(maximum_next) + 1
                    )
                    if slot_index == 0 and required_first_end_kwh is not None:
                        required_index = state_index(required_first_end_kwh)
                        next_indexes = (
                            (required_index,) if required_index in next_indexes else ()
                        )
                    for next_index in next_indexes:
                        energy_next = energies[next_index]
                        delta = energy_next - energy_now
                        charge_in = max(0.0, delta / eta_c)
                        discharge_out = max(0.0, -delta * eta_d)
                        if charge_in > max_charge_slot + 1e-9:
                            continue
                        if discharge_out > slot_discharge_limit + 1e-9:
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
                if costs[slot_count][energy_index][current_mode_index] >= inf:
                    continue
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
        if best is None:
            raise ValueError("optimizer has no feasible path")

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

    def _canonicalize(
        self,
        problem,
        prices,
        net_load,
        surplus,
        discharge_limit,
        actions,
        *,
        fixed_first_transition=False,
    ):
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
                if fixed_first_transition and source_index == 0:
                    continue
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
                discharge_limit[index],
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


class StochasticDynamicProgrammingOptimizer:
    """Choose common executable permissions with scenario-specific recourse."""

    def optimize(self, problem: OptimizationProblem) -> BatteryPlan:
        plan, _diagnostics = self.optimize_with_diagnostics(problem)
        return plan

    def optimize_with_diagnostics(
        self,
        problem: OptimizationProblem,
        *,
        required_first_policy: tuple[float, float] | None = None,
    ) -> tuple[BatteryPlan, dict[str, float]]:
        """Return the visible plan and its actual stochastic objective."""
        scenarios = problem.scenarios
        if scenarios is None:
            plan = DynamicProgrammingOptimizer().optimize(problem)
            return plan, {
                "deterministic_presentation_cost_eur": plan.optimized_cost_eur
            }
        prices = tuple(float(item.import_price_ct_per_kwh) for item in problem.market)
        export_prices = tuple(
            max(
                float(item.export_price_ct_per_kwh),
                float(problem.policy.export_opportunity_ct_per_kwh),
            )
            for item in problem.market
        )
        scenario_flows = tuple(
            (scenario.probability, _scenario_flows(scenario.slots, problem.ev_policy))
            for scenario in scenarios.scenarios
        )
        required_charge, discharge_budget, expected_cost, _expected_grid_equivalent = (
            self._common_first_policy(
                problem,
                prices,
                export_prices,
                scenario_flows,
                required_first_policy=required_first_policy,
            )
        )
        load = tuple(
            max(0.0, item.energy.p50_kwh) for item in problem.forecast.load.slots
        )
        pv = tuple(max(0.0, item.energy.p50_kwh) for item in problem.forecast.pv.slots)
        p50_net = tuple(
            max(0.0, demand - generation) for demand, generation in zip(load, pv)
        )
        p50_surplus = tuple(
            max(0.0, generation - demand) for demand, generation in zip(load, pv)
        )
        start_energy = _clamp(
            problem.constraints.capacity_kwh * problem.battery.soc_pct / 100.0,
            problem.constraints.capacity_kwh * problem.constraints.min_soc_pct / 100.0,
            problem.constraints.capacity_kwh * problem.constraints.max_soc_pct / 100.0,
        )
        eta = math.sqrt(problem.constraints.round_trip_efficiency)
        max_charge = problem.constraints.max_charge_power_w / 1000.0 * SLOT_H
        p50_pv_charge = p50_surplus[0] if problem.policy.pv_charging_allowed else 0.0
        first_charge = min(max_charge, max(required_charge, p50_pv_charge))
        first_discharge = (
            0.0 if first_charge > 1e-9 else min(discharge_budget, p50_net[0])
        )
        first_end = _clamp(
            start_energy + first_charge * eta - first_discharge / eta,
            problem.constraints.capacity_kwh * problem.constraints.min_soc_pct / 100.0,
            problem.constraints.capacity_kwh * problem.constraints.max_soc_pct / 100.0,
        )
        # Only the common required charge is a physical grid commitment. Extra
        # presentation charge follows forecast PV. Economic opportunity cost
        # from diverting PV away from an EV stays inside the objective and must
        # never be serialized as battery grid energy.
        planned_grid = min(first_charge, required_charge)
        planned_pv = first_charge - planned_grid
        first_mode = PlanMode.IDLE
        if first_charge > 1e-9:
            first_mode = PlanMode.CHARGE
        elif first_discharge > 1e-9:
            first_mode = PlanMode.DISCHARGE
        first = BatteryPlanSlot(
            slot=problem.forecast.load.slots[0].slot,
            mode=first_mode,
            pv_charge_allowed=problem.policy.pv_charging_allowed,
            grid_charge_allowed=problem.policy.grid_charging_allowed,
            planned_charge_kwh=first_charge,
            planned_discharge_kwh=first_discharge,
            required_charge_kwh=required_charge,
            discharge_budget_kwh=discharge_budget,
            expected_soc_start_pct=100.0
            * start_energy
            / problem.constraints.capacity_kwh,
            expected_soc_end_pct=100.0 * first_end / problem.constraints.capacity_kwh,
            planned_pv_charge_kwh=planned_pv,
            planned_grid_charge_kwh=planned_grid,
        )
        tail_slots = ()
        tail_cost = 0.0
        if len(problem.market) > 1:
            tail_forecast = replace(
                problem.forecast,
                load=replace(
                    problem.forecast.load, slots=problem.forecast.load.slots[1:]
                ),
                pv=replace(problem.forecast.pv, slots=problem.forecast.pv.slots[1:]),
                ev=replace(problem.forecast.ev, slots=problem.forecast.ev.slots[1:]),
            )
            tail_problem = replace(
                problem,
                problem_id=f"{problem.problem_id}:p50-recourse",
                forecast=tail_forecast,
                scenarios=None,
                market=problem.market[1:],
                battery=replace(
                    problem.battery,
                    soc_pct=100.0 * first_end / problem.constraints.capacity_kwh,
                ),
            )
            tail = DynamicProgrammingOptimizer().optimize(tail_problem)
            tail_slots = tail.slots
            tail_cost = tail.optimized_cost_eur
        p50_baseline = sum(
            (net * price - pv_surplus * export_price) / 100.0
            for net, pv_surplus, price, export_price in zip(
                p50_net, p50_surplus, prices, export_prices
            )
        )
        first_grid = max(0.0, p50_net[0] - first_discharge) + max(
            0.0, first_charge - p50_surplus[0]
        )
        first_export = max(0.0, p50_surplus[0] - first_charge)
        first_cost = (
            first_grid * prices[0]
            - first_export * export_prices[0]
            + first_discharge * problem.policy.min_margin_ct_per_kwh
        ) / 100.0
        plan = BatteryPlan(
            plan_id=f"{problem.problem_id}:{STOCHASTIC_OPTIMIZER_VERSION}",
            problem_id=problem.problem_id,
            generated_at_ms=problem.as_of_ms,
            optimizer_version=STOCHASTIC_OPTIMIZER_VERSION,
            constraints=problem.constraints,
            slots=(first, *tail_slots),
            baseline_cost_eur=p50_baseline,
            optimized_cost_eur=first_cost + tail_cost,
        )
        return plan, {
            "expected_scenario_cost_eur": expected_cost,
            "first_required_charge_kwh": required_charge,
            "first_discharge_budget_kwh": discharge_budget,
        }

    def _common_first_policy(
        self,
        problem,
        prices,
        export_prices,
        scenario_flows,
        *,
        required_first_policy=None,
    ):
        constraints = problem.constraints
        eta = math.sqrt(constraints.round_trip_efficiency)
        minimum = constraints.capacity_kwh * constraints.min_soc_pct / 100.0
        maximum = constraints.capacity_kwh * constraints.max_soc_pct / 100.0
        # Preserve feasible actions for low-power batteries instead of rounding
        # a whole slot's movement out of the coarser recourse lattice.
        max_charge_stored = constraints.max_charge_power_w / 1000.0 * SLOT_H * eta
        max_discharge_stored = constraints.max_discharge_power_w / 1000.0 * SLOT_H / eta
        recourse_step = min(
            STOCHASTIC_RECOURSE_STEP_KWH,
            ENERGY_STEP_KWH
            if min(max_charge_stored, max_discharge_stored)
            < STOCHASTIC_RECOURSE_STEP_KWH
            else STOCHASTIC_RECOURSE_STEP_KWH,
            maximum - minimum,
        )
        recourse_step = max(
            recourse_step,
            (maximum - minimum) / max(1, STOCHASTIC_MAX_RECOURSE_STATES - 1),
        )
        energies = _energy_lattice(minimum, maximum, recourse_step)

        start = _clamp(
            constraints.capacity_kwh * problem.battery.soc_pct / 100.0,
            minimum,
            maximum,
        )
        quantized_start = start
        recourse = tuple(
            (
                probability,
                _backward_recourse_values(
                    problem,
                    prices,
                    export_prices,
                    flows[0],
                    flows[1],
                    flows[2],
                    flows[3],
                    energies,
                    start_slot=1,
                ),
            )
            for probability, flows in scenario_flows
        )
        best = None
        max_charge = constraints.max_charge_power_w / 1000.0 * SLOT_H
        max_discharge = constraints.max_discharge_power_w / 1000.0 * SLOT_H
        charge_values = (
            _action_lattice(max_charge, ENERGY_STEP_KWH / eta)
            if problem.policy.grid_charging_allowed
            else (0.0,)
        )
        budget_values = (
            _action_lattice(max_discharge, ENERGY_STEP_KWH * eta)
            if problem.policy.discharge_allowed
            else (0.0,)
        )
        policies = tuple((charge, 0.0) for charge in charge_values) + tuple(
            (0.0, budget) for budget in budget_values[1:]
        )
        if required_first_policy is not None:
            required_charge, discharge_budget = required_first_policy
            if (
                required_charge < 0.0
                or discharge_budget < 0.0
                or required_charge > max_charge + 1e-9
                or discharge_budget > max_discharge + 1e-9
                or (required_charge > 1e-9 and discharge_budget > 1e-9)
            ):
                raise ValueError("required first policy is outside executable bounds")
            policies = ((required_charge, discharge_budget),)
        for required_charge, discharge_budget in policies:
            expected = 0.0
            feasible = True
            grid_charge = 0.0
            for (probability, flows), (_, future_values) in zip(
                scenario_flows, recourse, strict=True
            ):
                automatic_pv = (
                    flows[1][0] if problem.policy.pv_charging_allowed else 0.0
                )
                charge = min(max_charge, max(required_charge, automatic_pv))
                discharge = (
                    0.0
                    if charge > 1e-9
                    else min(discharge_budget, flows[3][0], max_discharge)
                )
                next_energy = _clamp(
                    quantized_start + charge * eta - discharge / eta,
                    minimum,
                    maximum,
                )
                actual_charge = (
                    max(0.0, (next_energy - quantized_start) / eta)
                    if charge > 0.0
                    else 0.0
                )
                actual_discharge = (
                    max(0.0, (quantized_start - next_energy) * eta)
                    if discharge > 0.0
                    else 0.0
                )
                step_cost = _transition_cost(
                    problem,
                    0,
                    actual_charge,
                    actual_discharge,
                    prices,
                    export_prices,
                    flows[0],
                    flows[1],
                    flows[2],
                    flows[3],
                )
                if step_cost is None:
                    feasible = False
                    break
                expected += probability * (
                    step_cost
                    + _interpolate_energy_value(energies, future_values, next_energy)
                )
                grid_charge += probability * max(0.0, actual_charge - flows[2][0])
            if not feasible:
                continue
            candidate = (
                expected,
                grid_charge,
                required_charge,
                discharge_budget,
            )
            if best is None or candidate < best:
                best = candidate
        if best is None:
            raise ValueError("stochastic optimizer has no feasible first transition")
        return best[2], best[3], best[0], best[1]


def _scenario_flows(slots, policy):
    demand = []
    charge_surplus = []
    physical_surplus = []
    discharge_limit = []
    ev_active_kwh = policy.ev_active_threshold_w / 1000.0 * SLOT_H
    for item in slots:
        house = max(0.0, item.house_load_kwh)
        pv = max(0.0, item.pv_generation_kwh)
        ev = max(0.0, item.ev_charge_kwh)
        slot_demand = max(0.0, house + ev - pv)
        free_surplus = max(0.0, pv - house - ev)
        slot_charge_surplus = (
            free_surplus if policy.pv_to_ev_first else max(0.0, pv - house)
        )
        active_ev = ev if ev >= ev_active_kwh else 0.0
        eligible = (
            slot_demand
            if policy.battery_may_feed_ev
            else max(0.0, slot_demand - active_ev)
        )
        if active_ev > 0.0 and not policy.discharge_during_ev_charging:
            eligible = 0.0
        demand.append(slot_demand)
        charge_surplus.append(slot_charge_surplus)
        physical_surplus.append(free_surplus)
        discharge_limit.append(eligible)
    return (
        tuple(demand),
        tuple(charge_surplus),
        tuple(physical_surplus),
        tuple(discharge_limit),
    )


def _backward_recourse_values(
    problem,
    prices,
    export_prices,
    demand,
    charge_surplus,
    physical_surplus,
    discharge_limit,
    energies,
    *,
    start_slot,
):
    constraints = problem.constraints
    eta = math.sqrt(constraints.round_trip_efficiency)
    minimum = energies[0]
    maximum = energies[-1]
    max_charge = constraints.max_charge_power_w / 1000.0 * SLOT_H
    max_discharge = constraints.max_discharge_power_w / 1000.0 * SLOT_H

    values = tuple(
        -problem.policy.terminal_value_ct_per_kwh * max(0.0, energy - minimum) / 100.0
        for energy in energies
    )
    for slot_index in range(len(prices) - 1, start_slot - 1, -1):
        next_values = values
        current = []
        for energy in energies:
            low = max(
                minimum,
                energy - min(max_discharge, discharge_limit[slot_index]) / eta,
            )
            high = min(maximum, energy + max_charge * eta)
            best = float("inf")
            lower_index = bisect_left(energies, low - 1e-9)
            upper_index = bisect_right(energies, high + 1e-9) - 1
            for next_index in range(lower_index, upper_index + 1):
                delta = energies[next_index] - energy
                charge = max(0.0, delta / eta)
                discharge = max(0.0, -delta * eta)
                cost = _transition_cost(
                    problem,
                    slot_index,
                    charge,
                    discharge,
                    prices,
                    export_prices,
                    demand,
                    charge_surplus,
                    physical_surplus,
                    discharge_limit,
                )
                if cost is not None:
                    best = min(best, cost + next_values[next_index])
            current.append(best)
        values = tuple(current)
    return values


def _transition_cost(
    problem,
    slot_index,
    charge,
    discharge,
    prices,
    export_prices,
    demand,
    charge_surplus,
    physical_surplus,
    discharge_limit,
):
    policy = problem.policy
    constraints = problem.constraints
    if charge > constraints.max_charge_power_w / 1000.0 * SLOT_H + 1e-9:
        return None
    if (
        discharge
        > min(
            discharge_limit[slot_index],
            constraints.max_discharge_power_w / 1000.0 * SLOT_H,
        )
        + 1e-9
    ):
        return None
    if discharge > 1e-9:
        if not policy.discharge_allowed:
            return None
        if (
            policy.discharge_floor_ct_per_kwh is not None
            and prices[slot_index] < policy.discharge_floor_ct_per_kwh - 1e-9
        ):
            return None
    if charge > 1e-9:
        if not (policy.pv_charging_allowed or policy.grid_charging_allowed):
            return None
        if (
            not policy.grid_charging_allowed
            and charge > charge_surplus[slot_index] + 1e-9
        ):
            return None
        if not policy.pv_charging_allowed and charge_surplus[slot_index] > 1e-9:
            return None
    # PV diverted from EV is permitted when battery priority is configured, but
    # it creates equal grid import and is therefore priced as grid energy.
    grid_charge = max(0.0, charge - physical_surplus[slot_index])
    if grid_charge > 1e-9:
        future_peak = max(prices[slot_index + 1 :], default=0.0)
        if future_peak * constraints.round_trip_efficiency < (
            prices[slot_index] + policy.min_margin_ct_per_kwh
        ):
            return None
    imported = max(0.0, demand[slot_index] - discharge) + grid_charge
    exported = max(0.0, physical_surplus[slot_index] - charge)
    return (
        imported * prices[slot_index]
        - exported * export_prices[slot_index]
        + discharge * policy.min_margin_ct_per_kwh
    ) / 100.0


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


def _energy_lattice(minimum: float, maximum: float, step: float) -> tuple[float, ...]:
    """Return an endpoint-safe monotone lattice within physical bounds."""
    if maximum <= minimum or step <= 0.0:
        return (minimum,)
    count = math.floor((maximum - minimum) / step + 1e-12)
    values = [minimum + index * step for index in range(count + 1)]
    if maximum - values[-1] > 1e-9:
        values.append(maximum)
    else:
        values[-1] = maximum
    return tuple(values)


def _action_lattice(maximum: float, step: float) -> tuple[float, ...]:
    """Return bounded action candidates including zero and the exact limit."""
    return _energy_lattice(0.0, max(0.0, maximum), step)


def _nearest_energy_index(energies: tuple[float, ...], value: float) -> int:
    position = bisect_left(energies, value)
    if position <= 0:
        return 0
    if position >= len(energies):
        return len(energies) - 1
    return (
        position - 1
        if value - energies[position - 1] <= energies[position] - value
        else position
    )


def _interpolate_energy_value(energies, values, energy):
    position = bisect_left(energies, energy)
    if position <= 0:
        return values[0]
    if position >= len(energies):
        return values[-1]
    low_energy, high_energy = energies[position - 1], energies[position]
    low_value, high_value = values[position - 1], values[position]
    if not math.isfinite(low_value) or not math.isfinite(high_value):
        return min(low_value, high_value)
    fraction = (energy - low_energy) / (high_energy - low_energy)
    return low_value * (1.0 - fraction) + high_value * fraction

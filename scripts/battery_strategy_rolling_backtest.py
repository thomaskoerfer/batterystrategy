#!/usr/bin/env python3
"""Run a local closed-loop comparison without touching runtime state or hardware."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

from custom_components.battery_strategy.contracts import (
    AutomaticDischargeMode,
    BatteryConstraints,
    BatteryPlan,
    BatteryState,
    CommandMode,
    CommercialPolicy,
    EvInteractionPolicy,
    LiveControlState,
    LiveMeasurements,
    LivePolicy,
    MarketSlot,
    OptimizationProblem,
    PlanCompilationState,
    PlanProgressBasis,
    SlotProgress,
)
from custom_components.battery_strategy.economic_optimizer import (
    OPTIMIZER_VERSION,
    UNIFIED_OPTIMIZER_VERSION,
    DynamicProgrammingOptimizer,
    UnifiedScenarioOptimizer,
)
from custom_components.battery_strategy.feature_store import CompressedFeatureStore
from custom_components.battery_strategy.plan_compiler import DeterministicPlanCompiler
from custom_components.battery_strategy.scenario_generation import (
    SCENARIO_RELEASE_VERSION,
)
from custom_components.battery_strategy.strategy import DeterministicLiveController
from scripts import battery_strategy_optimizer_shadow_backtest as scoring
from scripts.battery_strategy_scenario_replay import rebuild_trace

SLOT_MS = scoring.SLOT_MS


def select_window(traces, actuals, as_of_ms):
    """Choose the longest uninterrupted interval before inspecting outcomes."""
    selected = {}
    rejected = Counter()
    for trace in sorted(traces, key=lambda row: int(row["generated_at_ms"])):
        start = int(trace["load"]["slots"][0][0])
        if not 0 <= int(trace["generated_at_ms"]) - start <= 30_000:
            rejected["late_boundary"] += 1
            continue
        if trace.get("truncated"):
            rejected["truncated"] += 1
            continue
        if (
            scoring._usable_actual(actuals.get(start), start + SLOT_MS, as_of_ms)
            is None
        ):
            rejected["invalid_actual"] += 1
            continue
        if trace["optimization_problem"]["market"][0][3] != "tibber":
            rejected["nonfirm_settlement_price"] += 1
            continue
        selected.setdefault(start, trace)
    blocks = []
    previous_signature = None
    for start, trace in sorted(selected.items()):
        payload = trace["optimization_problem"]
        policy = payload["commercial_policy"]
        signature = json.dumps(
            [
                payload["constraints"],
                payload["ev_policy"],
                {
                    key: policy[key]
                    for key in (
                        "grid_charging_allowed",
                        "pv_charging_allowed",
                        "discharge_allowed",
                        "min_margin_ct_per_kwh",
                        "export_opportunity_ct_per_kwh",
                    )
                },
            ],
            sort_keys=True,
        )
        if (
            not blocks
            or start != blocks[-1][-1]["load"]["slots"][0][0] + SLOT_MS
            or signature != previous_signature
        ):
            blocks.append([])
        blocks[-1].append(trace)
        previous_signature = signature
    if not blocks:
        raise ValueError("no contiguous quality-valid observation window")
    return max(blocks, key=len), dict(rejected)


def captured_problem(trace, energy_kwh, forecast):
    """Keep historical information fixed; substitute only simulated battery state."""
    payload = trace["optimization_problem"]
    constraints = BatteryConstraints(**payload["constraints"])
    return OptimizationProblem(
        str(payload["problem_id"]),
        int(payload["as_of_ms"]),
        forecast,
        tuple(
            MarketSlot(slot.slot, float(row[1]), float(row[2]), str(row[3]))
            for slot, row in zip(forecast.load.slots, payload["market"], strict=True)
        ),
        BatteryState(
            int(payload["battery"]["captured_at_ms"]),
            100 * energy_kwh / constraints.capacity_kwh,
        ),
        constraints,
        CommercialPolicy(**payload["commercial_policy"]),
        ev_policy=EvInteractionPolicy(**payload["ev_policy"]),
    )


def dispatch_slot(plan, actual, energy_kwh, ev_policy, live_state, *, tick_ms=10_000):
    """Reuse compiler/live logic on slot-average exogenous powers and an ideal plant."""
    if tick_ms < 1:
        raise ValueError("tick_ms must be positive")
    slot = plan.slots[0].slot
    constraints = plan.constraints
    eta = math.sqrt(constraints.round_trip_efficiency)
    minimum = constraints.capacity_kwh * constraints.min_soc_pct / 100
    maximum = constraints.capacity_kwh * constraints.max_soc_pct / 100
    house_w = float(actual["house_load_no_ev_kwh"]) * 4000
    pv_w = float(actual["pv_generation_kwh"]) * 4000
    ev_w = float(actual["ev_charge_kwh"]) * 4000
    policy = LivePolicy(
        **asdict(ev_policy),
        # The live controller rounds emitted power to integer watts. A zero threshold
        # would turn a legitimate zero-load decision into an invalid active 0 W
        # command after rounding. This is not the unknown historical user deadband.
        min_command_power_w=1.0,
        max_charge_power_w=constraints.max_charge_power_w,
        max_discharge_power_w=constraints.max_discharge_power_w,
        automatic_discharge_mode=AutomaticDischargeMode.PRICE_SENSITIVE,
    )
    compiler = DeterministicPlanCompiler()
    controller = DeterministicLiveController()
    compilation = PlanCompilationState()
    basis = PlanProgressBasis(slot, plan.generated_at_ms, 0, 0)
    now = slot.start_ms
    charged = discharged = grid = exported = pv_charge = 0.0
    previous_charge_w = previous_discharge_w = 0.0
    while now < slot.end_ms:
        duration = min(tick_ms, slot.end_ms - now)
        if now < plan.generated_at_ms:
            duration = min(duration, plan.generated_at_ms - now)
            charge = discharge = 0.0
        else:
            progress = SlotProgress(
                slot, charged, discharged, 100 * energy_kwh / constraints.capacity_kwh
            )
            directive, compilation = compiler.compile(
                plan,
                progress,
                compilation,
                now,
                plan_progress_basis=basis,
            )
            net_w = house_w + ev_w - pv_w + previous_charge_w - previous_discharge_w
            measurements = LiveMeasurements(
                now,
                max(0.0, net_w),
                max(0.0, -net_w),
                pv_w,
                previous_charge_w,
                previous_discharge_w,
                ev_w,
                progress.soc_pct,
            )
            result = controller.command(directive, measurements, policy, live_state)
            live_state = result.state
            hours = duration / 3_600_000
            requested = result.command.power_w / 1000 * hours
            charge = discharge = 0.0
            if result.command.mode is CommandMode.INPUT:
                charge = min(requested, max(0.0, (maximum - energy_kwh) / eta))
                if result.command.reason == "must_charge":
                    charge = min(
                        charge,
                        max(
                            directive.required_charge_remaining_kwh,
                            result.diagnostics.pv_surplus_w / 1000 * hours,
                        ),
                    )
            elif result.command.mode is CommandMode.OUTPUT:
                discharge = min(
                    requested,
                    directive.discharge_budget_remaining_kwh,
                    max(0.0, (energy_kwh - minimum) * eta),
                )
        hours = duration / 3_600_000
        net = (house_w + ev_w - pv_w) / 1000 * hours + charge - discharge
        grid += max(0.0, net)
        exported += max(0.0, -net)
        pv_charge += min(charge, max(0.0, (pv_w - house_w - ev_w) / 1000 * hours))
        charged += charge
        discharged += discharge
        energy_kwh += charge * eta - discharge / eta
        previous_charge_w = charge / hours * 1000
        previous_discharge_w = discharge / hours * 1000
        now += duration
    return {
        "end_energy_kwh": energy_kwh,
        "charge_kwh": charged,
        "discharge_kwh": discharged,
        "grid_import_kwh": grid,
        "export_kwh": exported,
        "pv_charge_kwh": pv_charge,
        "loss_kwh": charged * (1 - eta) + discharged * (1 / eta - 1),
    }, live_state


def run_strategy(
    name,
    traces,
    actuals,
    bundles,
    scenario_results,
    initial_energy,
    output,
    tick_ms=10_000,
):
    """Keep battery, controller and economic state separate between strategies."""
    energy = initial_energy
    live = LiveControlState(CommandMode.IDLE, 0, None)
    rows = []
    status = Counter()
    for index, (trace, forecast, result) in enumerate(
        zip(traces, bundles, scenario_results, strict=True)
    ):
        problem = captured_problem(trace, energy, forecast)
        if name == "production":
            plan = DynamicProgrammingOptimizer().optimize(problem)
        else:
            problem = replace(problem, scenarios=result.scenarios)
            optimized = UnifiedScenarioOptimizer().optimize(problem)
            if optimized.decision is None:
                raise ValueError("shadow did not return an executable decision")
            plan = BatteryPlan(
                optimized.decision.decision_id,
                problem.problem_id,
                problem.as_of_ms,
                optimized.decision.optimizer_version,
                problem.constraints,
                (optimized.decision.slot,),
                0,
                0,
            )
            status[result.status.value] += 1
        slot = plan.slots[0].slot
        start_energy = energy
        flows, live = dispatch_slot(
            plan,
            actuals[slot.start_ms],
            energy,
            problem.ev_policy,
            live,
            tick_ms=tick_ms,
        )
        energy = flows["end_energy_kwh"]
        price = problem.market[0]
        rows.append(
            {
                "start_ms": slot.start_ms,
                "start_energy_kwh": start_energy,
                "required_charge_kwh": plan.slots[0].required_charge_kwh,
                "discharge_budget_kwh": plan.slots[0].discharge_budget_kwh,
                "scenario_status": result.status.value
                if name == "shadow"
                else "not_used",
                "bill_eur": (
                    flows["grid_import_kwh"] * price.import_price_ct_per_kwh
                    - flows["export_kwh"] * price.export_price_ct_per_kwh
                )
                / 100,
                **flows,
            }
        )
        if output is not None:
            with output.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"strategy": name, "index": index, **rows[-1]}) + "\n"
                )
        if index % 24 == 0:
            print(f"{name}: {index + 1}/{len(traces)}", flush=True)
    return rows, dict(status)


def perfect_foresight(
    traces, actuals, initial_energy, terminal_ct, *, end_energy_kwh=None
):
    """Independent continuous-energy MILP: ideal full-information economic bound."""
    import numpy as np
    import scipy
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_matrix

    n = len(traces)
    first = traces[0]["optimization_problem"]
    limits = BatteryConstraints(**first["constraints"])
    eta = math.sqrt(limits.round_trip_efficiency)
    lower = np.zeros(6 * n)
    upper = np.zeros(6 * n)
    objective = np.zeros(6 * n)
    integrality = np.zeros(6 * n)
    matrix = lil_matrix((5 * n, 6 * n))
    row_low = np.full(5 * n, -np.inf)
    row_high = np.full(5 * n, np.inf)
    for i, trace in enumerate(traces):
        # Per slot: AC charge, AC discharge, grid import/export, DC inventory,
        # and binary battery direction. Grid flow may change sign within a slot
        # when a finite charge commitment finishes before PV surplus ends.
        c, d, g, x, e, battery_mode = range(6 * i, 6 * i + 6)
        slot_start = int(trace["load"]["slots"][0][0])
        actual = actuals[slot_start]
        house = float(actual["house_load_no_ev_kwh"])
        pv = float(actual["pv_generation_kwh"])
        ev = float(actual["ev_charge_kwh"])
        payload = trace["optimization_problem"]
        policy = payload["commercial_policy"]
        ep = EvInteractionPolicy(**payload["ev_policy"])
        demand = max(0.0, house + ev - pv)
        active_ev = ev if ev >= ep.ev_active_threshold_w / 4000 else 0.0
        eligible = demand if ep.battery_may_feed_ev else max(0.0, demand - active_ev)
        if active_ev and not ep.discharge_during_ev_charging:
            eligible = 0.0
        max_charge = limits.max_charge_power_w / 4000
        charge_surplus = max(0.0, pv - house - (ev if ep.pv_to_ev_first else 0))
        if not policy["pv_charging_allowed"] and charge_surplus > 1e-9:
            max_charge = 0.0
        if not policy["grid_charging_allowed"]:
            max_charge = min(max_charge, charge_surplus)
            if not policy["pv_charging_allowed"]:
                max_charge = 0.0
        max_discharge = min(eligible, limits.max_discharge_power_w / 4000)
        if not policy["discharge_allowed"]:
            max_discharge = 0.0
        big = house + pv + ev + max_charge + max_discharge + 1
        upper[[c, d, g, x, e, battery_mode]] = [
            max_charge,
            max_discharge,
            big,
            big,
            limits.capacity_kwh * limits.max_soc_pct / 100,
            1,
        ]
        lower[e] = limits.capacity_kwh * limits.min_soc_pct / 100
        integrality[battery_mode] = 1
        market = payload["market"][0]
        objective[g], objective[x] = float(market[1]) / 100, -float(market[2]) / 100
        objective[d] = float(policy["min_margin_ct_per_kwh"]) / 100
        r = i * 5
        matrix[r, [g, x, c, d]] = [1, -1, -1, 1]
        row_low[r] = row_high[r] = house + ev - pv
        matrix[r + 1, [e, c, d]] = [1, -eta, 1 / eta]
        if i:
            matrix[r + 1, e - 6] = -1
            row_low[r + 1] = row_high[r + 1] = 0
        else:
            row_low[r + 1] = row_high[r + 1] = initial_energy
        for offset, columns, values, bound in (
            (2, [c, battery_mode], [1, -max_charge], 0),
            (3, [d, battery_mode], [1, max_discharge], max_discharge),
        ):
            matrix[r + offset, columns] = values
            row_high[r + offset] = bound
        if pv > house + ev:
            # With constant exogenous power, full-power charging for a fraction
            # c/max_charge of the slot maximizes possible paid import. This
            # bounds import/export cycling even at negative import prices.
            surplus = pv - house - ev
            import_per_charge = (
                max(0.0, 1 - surplus / max_charge)
                if max_charge > 0 and policy["grid_charging_allowed"]
                else 0.0
            )
            matrix[r + 4, [g, c]] = [1, -import_per_charge]
            row_high[r + 4] = 0
        else:
            upper[x] = 0  # Battery export is forbidden; only PV can be exported.
        # The ideal bound ignores scheduler latency, hysteresis and start thresholds.
    objective[6 * (n - 1) + 4] = -terminal_ct / 100
    if end_energy_kwh is not None:
        minimum = limits.capacity_kwh * limits.min_soc_pct / 100
        maximum = limits.capacity_kwh * limits.max_soc_pct / 100
        if (
            not math.isfinite(end_energy_kwh)
            or not minimum <= end_energy_kwh <= maximum
        ):
            raise ValueError("fixed final inventory is outside battery bounds")
        lower[6 * (n - 1) + 4] = end_energy_kwh
        upper[6 * (n - 1) + 4] = end_energy_kwh
    solved = milp(
        objective,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=LinearConstraint(matrix.tocsc(), row_low, row_high),
        options={"time_limit": 120, "mip_rel_gap": 1e-8},
    )
    if not solved.success:
        raise ValueError(f"perfect-foresight solve failed: {solved.message}")
    rows = []
    for i, trace in enumerate(traces):
        c, d, g, x, e = map(float, solved.x[6 * i : 6 * i + 5])
        price = trace["optimization_problem"]["market"][0]
        rows.append(
            {
                "start_ms": int(trace["load"]["slots"][0][0]),
                "charge_kwh": c,
                "discharge_kwh": d,
                "grid_import_kwh": g,
                "export_kwh": x,
                "end_energy_kwh": e,
                "bill_eur": (g * float(price[1]) - x * float(price[2])) / 100,
                "loss_kwh": c * (1 - eta) + d * (1 / eta - 1),
            }
        )
    return rows, {
        "mip_gap": float(solved.mip_gap),
        "status": solved.message,
        "scipy_version": scipy.__version__,
        "numpy_version": np.__version__,
    }


def summarize(rows, initial_energy, terminal_ct, margin_ct, ev_baseline_cost):
    bill = sum(row["bill_eur"] for row in rows)
    discharge = sum(row["discharge_kwh"] for row in rows)
    end = rows[-1]["end_energy_kwh"]
    inventory_delta = end - initial_energy
    return {
        "bill_including_ev_eur": bill,
        "bill_excluding_fixed_ev_baseline_eur": bill - ev_baseline_cost,
        "charge_kwh": sum(row["charge_kwh"] for row in rows),
        "discharge_kwh": discharge,
        "grid_import_kwh": sum(row["grid_import_kwh"] for row in rows),
        "export_kwh": sum(row["export_kwh"] for row in rows),
        "loss_kwh": sum(row["loss_kwh"] for row in rows),
        "end_energy_kwh": end,
        "inventory_adjusted_bill_eur": bill - inventory_delta * terminal_ct / 100,
        "economic_objective_eur": bill
        + discharge * margin_ct / 100
        - inventory_delta * terminal_ct / 100,
    }


def verify_benchmark(strategies, simulated_rows, *, tolerance_eur=1e-6):
    """Fail visibly if a purported ideal bound does not bound the rolling paths."""
    ideal = strategies["perfect_foresight"]["economic_objective_eur"]
    checks = {}
    for name, rows in simulated_rows.items():
        mixed = sum(min(row["charge_kwh"], row["discharge_kwh"]) > 1e-8 for row in rows)
        regret = strategies[name]["economic_objective_eur"] - ideal
        if mixed or not math.isfinite(regret) or regret < -tolerance_eur:
            raise ValueError(
                f"perfect-foresight bound invalid for {name}: mixed={mixed}, regret={regret}"
            )
        checks[name] = {
            "mixed_battery_slots": mixed,
            "mixed_grid_slots": sum(
                min(row["grid_import_kwh"], row["export_kwh"]) > 1e-8 for row in rows
            ),
            "regret_eur": max(0.0, regret),
        }
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--feature-store", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--start-ms", type=int, required=True)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--as-of-ms", type=int, required=True)
    parser.add_argument("--tick-ms", type=int, default=10_000)
    args = parser.parse_args()
    if not 1 <= args.tick_ms <= SLOT_MS:
        parser.error("--tick-ms must be between 1 and one slot")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    traces = scoring.load_traces(args.trace_dir, args.start_ms, args.end_ms)
    actuals = scoring.load_actuals(args.feature_store)
    window, exclusions = select_window(traces, actuals, args.as_of_ms)
    store = CompressedFeatureStore(args.feature_store)
    history, _schema = (
        store._read()
    )  # Read only: never initialize/migrate the source snapshot.
    bundles, results, fidelities = [], [], Counter()
    from scripts.battery_strategy_scenario_replay import _bundle

    for trace in window:
        bundle, complete = _bundle(trace)
        if not complete:
            raise ValueError(
                "incomplete component forecast cannot enter this rolling replay"
            )
        fidelity, result = rebuild_trace(
            trace, history, fallback_timezone=args.timezone
        )
        bundles.append(bundle)
        results.append(result)
        fidelities[fidelity] += 1
    initial_payload = window[0]["optimization_problem"]
    initial = (
        initial_payload["battery"]["soc_pct"]
        * initial_payload["constraints"]["capacity_kwh"]
        / 100
    )
    terminal = float(
        window[-1]["optimization_problem"]["commercial_policy"][
            "terminal_value_ct_per_kwh"
        ]
    )
    margin = float(initial_payload["commercial_policy"]["min_margin_ct_per_kwh"])
    # Price attribution removes the same immutable EV baseline from every strategy.
    ev_cost = sum(
        max(
            0.0,
            actuals[int(t["load"]["slots"][0][0])]["ev_charge_kwh"]
            - max(
                0.0,
                actuals[int(t["load"]["slots"][0][0])]["pv_generation_kwh"]
                - actuals[int(t["load"]["slots"][0][0])]["house_load_no_ev_kwh"],
            ),
        )
        * float(t["optimization_problem"]["market"][0][1])
        / 100
        for t in window
    )
    manifest = {
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tick_ms": args.tick_ms,
        "control_mode": "price_sensitive",
        "command_quantization_floor_w": 1.0,
        "optimizer_versions": {
            "production": OPTIMIZER_VERSION,
            "shadow": UNIFIED_OPTIMIZER_VERSION,
        },
        "scenario_model_version": SCENARIO_RELEASE_VERSION,
        "source_hashes": {
            relative: hashlib.sha256(Path(relative).read_bytes()).hexdigest()
            for relative in (
                "custom_components/battery_strategy/economic_optimizer.py",
                "custom_components/battery_strategy/scenario_generation/engine.py",
                "custom_components/battery_strategy/plan_compiler.py",
                "custom_components/battery_strategy/strategy.py",
                "scripts/battery_strategy_scenario_replay.py",
            )
        },
        "slots": len(window),
        "start_ms": int(window[0]["load"]["slots"][0][0]),
        "end_ms": int(window[-1]["load"]["slots"][0][1]),
        "initial_energy_kwh": initial,
        "terminal_ct_per_stored_kwh": terminal,
        "margin_ct_per_discharge_kwh": margin,
        "exclusions": exclusions,
        "scenario_fidelity": dict(fidelities),
        "feature_sha256": hashlib.sha256(args.feature_store.read_bytes()).hexdigest(),
        "trace_sha256": hashlib.sha256(
            json.dumps(window, sort_keys=True).encode()
        ).hexdigest(),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    report = {"non_authoritative": True, "window": manifest, "strategies": {}}
    started = time.monotonic()
    simulated_rows = {}
    with ProcessPoolExecutor(max_workers=2) as pool:
        jobs = {
            name: pool.submit(
                run_strategy,
                name,
                window,
                actuals,
                bundles,
                results,
                initial,
                args.output_dir / f"{name}-slots.jsonl",
                args.tick_ms,
            )
            for name in ("production", "shadow")
        }
        for name, job in jobs.items():
            rows, statuses = job.result()
            simulated_rows[name] = rows
            report["strategies"][name] = summarize(
                rows, initial, terminal, margin, ev_cost
            )
            report["strategies"][name]["scenario_status"] = statuses
            (args.output_dir / "report.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
    oracle, solver = perfect_foresight(window, actuals, initial, terminal)
    report["strategies"]["perfect_foresight"] = summarize(
        oracle, initial, terminal, margin, ev_cost
    )
    report["perfect_foresight_solver"] = solver
    report["benchmark_checks"] = verify_benchmark(report["strategies"], simulated_rows)
    (args.output_dir / "perfect-slots.json").write_text(json.dumps(oracle) + "\n")
    report["runtime_s"] = time.monotonic() - started
    report["terminal_value_sensitivity"] = {
        str(value): {
            name: result["bill_including_ev_eur"]
            + result["discharge_kwh"] * margin / 100
            - (result["end_energy_kwh"] - initial) * value / 100
            for name, result in report["strategies"].items()
            if name != "perfect_foresight"
        }
        for value in sorted(
            {0.0, terminal, float(window[-1]["optimization_problem"]["market"][0][1])}
        )
    }
    report["limitations"] = [
        "Slot-average plant: intra-slot power peaks, cloud/EV timing and device/BMS losses beyond configured RTE are unknown.",
        "One captured boundary plan per slot; no counterfactual mid-slot replans, manual overrides or sensor outages.",
        "Forecasts and commercial policy (including recorded inventory floor) are held fixed; only simulated SoC changes.",
        "Retrospective scenario reconstruction may include later history repairs or component enrichment; this is not an exact historic online shadow or release-gate pass.",
        "Normal P50 fallbacks remain in the closed-loop candidate; scenario success and fallback counts are reported rather than selectively deleting weak slots.",
        "Rolling forecasts retain their original planning horizon beyond the scoring cutoff; the terminal scalar approximates that unobserved continuation for scoring only.",
        "Control is enabled and price-sensitive for both strategies; no historical control-switch state is inferred.",
        "A 1 W command-quantization floor is assumed, not the historical operator deadband.",
        "Perfect foresight is an ideal continuous-energy bound, without controller timing; residual inventory and margin are valued identically.",
        "EV stays separate and present physically; its fixed no-battery baseline cost is subtracted identically, not modeled as extra house demand.",
    ]
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()

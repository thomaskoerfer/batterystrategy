"""Helpers for evaluating parallel operation against the reference data."""

from __future__ import annotations

from dataclasses import dataclass

from .models import StrategyCommand
from .plan_models import PlanComparison, StrategyPlan

INPUT_TOLERANCE_W = 150


@dataclass(frozen=True)
class ParallelEvaluation:
    """Result of comparing HACS and reference data/commands."""

    samples: int
    matching_mode_samples: int
    max_power_delta_w: int
    input_samples: int = 0
    max_house_load_no_ev_delta_w: int = 0
    max_house_load_total_delta_w: int = 0
    max_pv_delta_w: int = 0
    max_residual_no_ev_delta_w: int = 0
    max_residual_with_ev_delta_w: int = 0

    @property
    def mode_match_ratio(self) -> float:
        """Return share of samples with matching command modes."""
        if self.samples <= 0:
            return 0.0
        return self.matching_mode_samples / self.samples

    @property
    def command_passed(self) -> bool:
        """Return whether final command decisions match closely."""
        return self.samples >= 12 and self.mode_match_ratio >= 0.95 and self.max_power_delta_w <= 100

    @property
    def input_passed(self) -> bool:
        """Return whether the shared data points match closely."""
        return (
            self.input_samples >= 12
            and self.max_house_load_no_ev_delta_w <= INPUT_TOLERANCE_W
            and self.max_house_load_total_delta_w <= INPUT_TOLERANCE_W
            and self.max_pv_delta_w <= INPUT_TOLERANCE_W
            and self.max_residual_no_ev_delta_w <= INPUT_TOLERANCE_W
            and self.max_residual_with_ev_delta_w <= INPUT_TOLERANCE_W
        )

    @property
    def passed(self) -> bool:
        """Return the data-consistency pass state for parallel operation."""
        return self.input_passed


def evaluate_parallel_commands(
    new_commands: list[StrategyCommand],
    reference_modes: list[str],
    reference_powers_w: list[float],
    new_data_points: list[dict[str, float]] | None = None,
    reference_data_points: list[dict[str, float]] | None = None,
) -> ParallelEvaluation:
    """Compare new data points and final commands with the reference data."""
    n = min(len(new_commands), len(reference_modes), len(reference_powers_w))

    matching = 0
    max_delta = 0
    if n > 0:
        for i in range(n):
            if new_commands[i].mode == reference_modes[i]:
                matching += 1
            max_delta = max(max_delta, abs(int(round(new_commands[i].power_w - float(reference_powers_w[i])))))

    input_n = min(len(new_data_points or []), len(reference_data_points or []))
    deltas = {
        "house_load_no_ev_w": 0,
        "house_load_total_w": 0,
        "pv_w": 0,
        "residual_no_ev_w": 0,
        "residual_with_ev_w": 0,
    }
    for i in range(input_n):
        new = (new_data_points or [])[i]
        reference = (reference_data_points or [])[i]
        for key in deltas:
            deltas[key] = max(deltas[key], abs(int(round(float(new[key]) - float(reference[key])))))

    return ParallelEvaluation(
        samples=n,
        matching_mode_samples=matching,
        max_power_delta_w=max_delta,
        input_samples=input_n,
        max_house_load_no_ev_delta_w=deltas["house_load_no_ev_w"],
        max_house_load_total_delta_w=deltas["house_load_total_w"],
        max_pv_delta_w=deltas["pv_w"],
        max_residual_no_ev_delta_w=deltas["residual_no_ev_w"],
        max_residual_with_ev_delta_w=deltas["residual_with_ev_w"],
    )


def compare_optimizer_plan(
    plan: StrategyPlan,
    reference_output: dict,
    command_mode: str,
    command_power_w: float,
    live_mode: str,
    live_power_w: float,
) -> PlanComparison:
    """Compare new optimizer plan with reference profile attributes."""
    reference_tomorrow_power = _profile_map(reference_output.get("profile_tomorrow_power"))
    reference_tomorrow_charge = _profile_map(reference_output.get("profile_tomorrow_charge_power"))
    reference_tomorrow_discharge = _profile_map(reference_output.get("profile_tomorrow_discharge_power"))
    reference_tomorrow_soc = _profile_map(reference_output.get("profile_tomorrow_soc"))
    reference_48h_charge = _profile_map(reference_output.get("profile_48h_charge_fc_power"))
    reference_48h_discharge = _profile_map(reference_output.get("profile_48h_discharge_fc_power"))
    reference_48h_load = _profile_map(reference_output.get("profile_48h_house_fc_power"))
    reference_48h_pv = _profile_map(reference_output.get("profile_48h_pv_fc_power"))
    if not reference_tomorrow_charge and not reference_tomorrow_discharge:
        reference_tomorrow_charge = reference_48h_charge
        reference_tomorrow_discharge = reference_48h_discharge

    tomorrow = _tomorrow_date(plan)
    tomorrow_points = [p for p in plan.points if p.date == tomorrow]
    tom_power_deltas = []
    tom_soc_deltas = []
    for point in tomorrow_points:
        reference_charge = _nearest(reference_tomorrow_charge, point.ts_ms)
        reference_discharge = _nearest(reference_tomorrow_discharge, point.ts_ms)
        if reference_charge is not None or reference_discharge is not None:
            tom_power_deltas.append(
                max(
                    abs(point.charge_fc_w - float(reference_charge or 0.0)),
                    abs(point.discharge_fc_w - float(reference_discharge or 0.0)),
                )
            )
        else:
            reference_power = _nearest(reference_tomorrow_power, point.ts_ms)
            if reference_power is not None:
                tom_power_deltas.append(abs(point.power_w - abs(reference_power)))
        reference_soc = _nearest(reference_tomorrow_soc, point.ts_ms)
        if reference_soc is not None:
            tom_soc_deltas.append(abs(point.soc_pct - reference_soc))

    power_48h_deltas = []
    load_deltas = []
    pv_deltas = []
    first_ts = plan.points[0].ts_ms if plan.points else 0
    for point in plan.points:
        if point.ts_ms - first_ts < 60 * 60 * 1000:
            continue
        reference_charge = _nearest(reference_48h_charge, point.ts_ms) or 0.0
        reference_discharge = _nearest(reference_48h_discharge, point.ts_ms) or 0.0
        if reference_48h_charge or reference_48h_discharge:
            power_48h_deltas.append(abs(point.charge_fc_w - point.discharge_fc_w - reference_charge + reference_discharge))
        reference_load = _nearest(reference_48h_load, point.ts_ms)
        if reference_load is not None:
            load_deltas.append(abs(point.load_fc_w - reference_load))
        reference_pv = _nearest(reference_48h_pv, point.ts_ms)
        if reference_pv is not None:
            pv_deltas.append(abs(point.pv_fc_w - reference_pv))

    max_tom_power = int(round(max(tom_power_deltas or [0])))
    max_48h_power = int(round(max(power_48h_deltas or [0])))
    max_load = int(round(max(load_deltas or [0])))
    max_pv = int(round(max(pv_deltas or [0])))
    live_power_delta = abs(float(command_power_w or 0) - float(live_power_w or 0))
    live_command_passed = True if plan.override_active else (command_mode == live_mode and live_power_delta <= 150)

    return PlanComparison(
        plan_input_passed=bool(load_deltas or pv_deltas) and max_load <= 250 and max_pv <= 350,
        tomorrow_strategy_passed=bool(tom_power_deltas) and max_tom_power <= 250 and max(tom_soc_deltas or [0.0]) <= 10.0,
        forty8h_strategy_passed=bool(power_48h_deltas) and max_48h_power <= 300,
        live_command_passed=bool(live_command_passed),
        override_active=plan.override_active,
        samples_tomorrow=len(tom_power_deltas),
        samples_48h=len(power_48h_deltas),
        max_tomorrow_power_delta_w=max_tom_power,
        max_48h_power_delta_w=max_48h_power,
        max_tomorrow_soc_delta_pct=round(max(tom_soc_deltas or [0.0]), 2),
        max_forecast_load_delta_w=max_load,
        max_forecast_pv_delta_w=max_pv,
    )


def _profile_map(profile) -> dict[int, float]:
    result = {}
    for item in profile or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            result[int(float(item[0]))] = float(item[1])
        except (TypeError, ValueError):
            continue
    return result


def _nearest(series: dict[int, float], ts_ms: int, tolerance_ms: int = 20 * 60 * 1000) -> float | None:
    if not series:
        return None
    best_ts = min(series, key=lambda item_ts: abs(item_ts - ts_ms))
    if abs(best_ts - ts_ms) > tolerance_ms:
        return None
    return series[best_ts]


def _tomorrow_date(plan: StrategyPlan) -> str | None:
    if not plan.points:
        return None
    dates = []
    for point in plan.points:
        if point.date not in dates:
            dates.append(point.date)
    return dates[1] if len(dates) > 1 else dates[0]

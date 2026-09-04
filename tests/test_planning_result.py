"""Typed planning-result and persistence migration tests."""

from __future__ import annotations

import datetime as dt

import pytest

from custom_components.battery_strategy.models import StrategyOptions
from custom_components.battery_strategy.plan_models import PlanPoint, StrategyPlan
from custom_components.battery_strategy.planning_result import (
    PERSISTED_PLAN_KEY,
    build_planning_result,
    persisted_output,
    result_from_persisted_output,
)
from tests.plan_helpers import canonical_plan


def _result_fixture():
    start_ms = 1_800_000_000_000
    options = StrategyOptions()
    operator_plan = StrategyPlan(
        points=[
            PlanPoint(
                ts_ms=start_ms,
                date="2027-01-15",
                price_ct=30.0,
                load_fc_w=800,
                pv_fc_w=0,
                grid_import_fc_w=0,
                grid_export_fc_w=0,
                grid_net_fc_w=0,
                mode="output",
                power_w=800,
                charge_fc_w=0,
                discharge_fc_w=800,
                soc_pct=50.0,
                discharge_budget_kwh=0.2,
            )
        ],
        current_mode="output",
        current_power_w=800,
        reason="test",
    )
    plan = canonical_plan(operator_plan, options, start_ms)
    result = build_planning_result(
        plan,
        {
            "profile_48h_price": [[start_ms, 30.0]],
            "profile_48h_discharge_fc_power": [[start_ms, 800.0]],
            "profile_48h_discharge_budget_kwh": [[start_ms, 0.2]],
            "profile_today_soc": [[start_ms, 50.0]],
        },
        timezone=dt.timezone.utc,
        now_ms=start_ms,
        override_active=False,
    )
    return result, options, start_ms


def test_canonical_plan_round_trips_without_using_operator_profiles():
    result, options, start_ms = _result_fixture()

    restored = result_from_persisted_output(
        persisted_output(result, options),
        options,
        timezone=dt.timezone.utc,
        now_ms=start_ms,
    )

    assert restored.battery_plan == result.battery_plan
    assert restored.operator_plan.points[0].discharge_fc_w == 800


def test_legacy_operator_snapshot_remains_visible_but_cannot_authorize_control():
    _, options, start_ms = _result_fixture()
    legacy = {
        "profile_48h_price": [[start_ms, 30.0]],
        "profile_48h_discharge_fc_power": [[start_ms, 800.0]],
        "profile_48h_discharge_budget_kwh": [[start_ms, 0.2]],
        "profile_today_soc": [[start_ms, 50.0]],
    }

    restored = result_from_persisted_output(
        legacy, options, timezone=dt.timezone.utc, now_ms=start_ms
    )

    assert restored.battery_plan is None
    assert restored.operator_plan.points[0].discharge_fc_w == 800


def test_corrupt_canonical_plan_fails_closed_instead_of_partially_loading():
    result, options, start_ms = _result_fixture()
    stored = persisted_output(result, options)
    stored[PERSISTED_PLAN_KEY]["slots"].append("invalid")

    restored = result_from_persisted_output(
        stored, options, timezone=dt.timezone.utc, now_ms=start_ms
    )

    assert restored.battery_plan is None


def test_changed_physical_constraints_invalidate_persisted_plan():
    result, _, start_ms = _result_fixture()

    restored = result_from_persisted_output(
        persisted_output(result, StrategyOptions()),
        StrategyOptions(battery_capacity_kwh=8.0),
        timezone=dt.timezone.utc,
        now_ms=start_ms,
    )

    assert restored.battery_plan is None


def test_changed_execution_policy_invalidates_persisted_plan():
    result, options, start_ms = _result_fixture()

    restored = result_from_persisted_output(
        persisted_output(result, options),
        StrategyOptions(grid_charging="price_sensitive"),
        timezone=dt.timezone.utc,
        now_ms=start_ms,
    )

    assert restored.battery_plan is None


def test_planning_result_collections_are_immutable():
    result, _, _ = _result_fixture()

    assert isinstance(result.operator_plan.points, tuple)
    with pytest.raises(TypeError):
        result.operator_data["mode"] = "input"
    with pytest.raises(TypeError):
        result.operator_data["profile_48h_price"][0][1] = 99.0
    with pytest.raises(TypeError):
        result.operator_plan.daily_costs["2027-01-15"] = object()

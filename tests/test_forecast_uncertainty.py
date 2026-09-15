"""Forecast uncertainty regressions at the approved forecasting seams."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

from custom_components.battery_strategy.component_config import LoadComponentSpec
from custom_components.battery_strategy.const import LOAD_PROFILE_AIR_CONDITIONING
from custom_components.battery_strategy.contracts import (
    BatteryConstraints,
    BatteryState,
    CommercialPolicy,
    DataQuality,
    ForecastRequest,
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadForecastContext,
    MarketSlot,
    OptimizationProblem,
    PvPlant,
    SlotKey,
)
from custom_components.battery_strategy.economic_optimizer import (
    DynamicProgrammingOptimizer,
)
from custom_components.battery_strategy.forecast_application import (
    ProductionForecastConfig,
    ProductionForecastModule,
)
from custom_components.battery_strategy.forecast_calibration import (
    calibration_from_state,
    mature_predictions,
    queue_predictions,
)
from custom_components.battery_strategy.forecasting.uncertainty import (
    PV_SERIES,
    TOTAL_LOAD_SERIES,
    ForecastResidualCalibration,
    calibrated_quantile_energy,
    cohort_key,
    component_series,
)
from custom_components.battery_strategy.planning_state import (
    ForecastLearningState,
    PlanningStateStore,
)
from tests.planning_runtime_helpers import settings_from_values

SLOT_MS = 15 * 60 * 1000


def _history(as_of: dt.datetime, days: int = 35) -> tuple[HistoricalFeatureSlot, ...]:
    first = as_of.astimezone(dt.UTC) - dt.timedelta(days=days)
    first = first.replace(minute=0, second=0, microsecond=0)
    result = []
    for index in range(days * 96):
        start = first + dt.timedelta(minutes=15 * index)
        local = start.astimezone(ZoneInfo("Europe/Berlin"))
        quarter = local.hour * 4 + local.minute // 15
        general = 0.08 + (quarter % 4) * 0.01
        component = 0.04 if 68 <= quarter <= 76 else 0.0
        pv = max(0.0, 0.4 - abs(quarter - 52) * 0.025)
        slot = SlotKey(
            int(start.timestamp() * 1000),
            int((start + dt.timedelta(minutes=15)).timestamp() * 1000),
        )
        result.append(
            HistoricalFeatureSlot(
                slot=slot,
                house_load_no_ev_kwh=general + component,
                pv_generation_kwh=pv,
                grid_import_kwh=max(0.0, general + component - pv),
                grid_export_kwh=max(0.0, pv - general - component),
                battery_charge_kwh=0.0,
                battery_discharge_kwh=0.0,
                ev_charge_kwh=0.0,
                price_ct_per_kwh=30.0,
                quality=DataQuality(),
                load_components=(LoadComponentEnergy("air_conditioning", component),),
            )
        )
    return tuple(result)


def _forecast(
    history: tuple[HistoricalFeatureSlot, ...],
    uncertainty: ForecastResidualCalibration | None = None,
):
    timezone = ZoneInfo("Europe/Berlin")
    as_of = dt.datetime(2026, 9, 15, 18, 0, tzinfo=timezone)
    start_ms = int(as_of.astimezone(dt.UTC).timestamp() * 1000)
    request = ForecastRequest(
        start_ms,
        "Europe/Berlin",
        tuple(
            SlotKey(start_ms + index * SLOT_MS, start_ms + (index + 1) * SLOT_MS)
            for index in range(8)
        ),
    )
    return (
        ProductionForecastModule()
        .forecast(
            request,
            history,
            LoadForecastContext(500.0),
            (),
            PvPlant(2.3, 2.0),
            ProductionForecastConfig(
                load_bias=1.0,
                load_slot_biases=(1.0,) * 96,
                pv_global_bias=1.0,
                pv_slot_biases=(1.0,) * 96,
                current_weather_factor=0.8,
                current_pv_w=None,
                tomorrow_energy_kwh=None,
                uncertainty=uncertainty or ForecastResidualCalibration({}),
            ),
            (LoadComponentSpec("air_conditioning", LOAD_PROFILE_AIR_CONDITIONING),),
        )
        .bundle
    )


def test_empirical_interval_is_anchored_to_the_unchanged_point_forecast():
    energy = calibrated_quantile_energy(0.5, tuple(float(value) for value in range(20)))

    assert energy.p50_kwh == 0.5
    assert energy.p10_kwh is not None
    assert energy.p10_kwh <= energy.p50_kwh <= energy.p90_kwh
    assert energy.calibration_samples == 20


def test_insufficient_or_future_free_evidence_does_not_invent_quantiles():
    energy = calibrated_quantile_energy(0.25, (0.1,) * 11)

    assert energy.p50_kwh == 0.25
    assert energy.p10_kwh is None
    assert energy.p90_kwh is None
    assert energy.calibration_samples == 0


def test_only_issued_forecasts_mature_into_lead_specific_residuals():
    target = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
    target_ms = int(target.timestamp() * 1000)
    actual = HistoricalFeatureSlot(
        slot=SlotKey(target_ms, target_ms + SLOT_MS),
        house_load_no_ev_kwh=0.7,
        pv_generation_kwh=0.0,
        grid_import_kwh=0.7,
        grid_export_kwh=0.0,
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        ev_charge_kwh=0.0,
        price_ct_per_kwh=30.0,
        quality=DataQuality(),
        load_components=(LoadComponentEnergy("air_conditioning", 0.0),),
    )
    state = ForecastLearningState(
        quantile_pending=[
            {
                "target_ms": target_ms,
                "target_end_ms": target_ms + SLOT_MS,
                "generated_at_ms": target_ms - 2 * 60 * 60 * 1000,
                "lead_bucket": 1,
                "series": {
                    TOTAL_LOAD_SERIES: ["model-v1", 0.2],
                    component_series("air_conditioning"): ["ac-v1", 0.3],
                },
            }
        ]
    )

    mature_predictions(
        state,
        (actual,),
        as_of_ms=target_ms + SLOT_MS,
        timezone="Europe/Berlin",
    )

    load_key = cohort_key(TOTAL_LOAD_SERIES, "model-v1", 1)
    component_key = cohort_key(component_series("air_conditioning"), "ac-v1", 1)
    assert state.quantile_pending == []
    assert state.quantile_residuals[load_key] == [0.5]
    # A false-positive event remains evidence instead of being conditioned away.
    assert state.quantile_residuals[component_key] == [-0.3]


def test_production_forecast_populates_independent_quantiles_without_changing_p50():
    as_of = dt.datetime(2026, 9, 15, 18, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    history = _history(as_of)
    point = _forecast(history)
    versions = {
        TOTAL_LOAD_SERIES: point.load.model_version,
        PV_SERIES: point.pv.model_version,
        **{
            component_series(component.component_key): component.model_version
            for component in point.load.components
        },
    }
    cohorts = {}
    for series_key, version in versions.items():
        for bucket in range(4):
            cohorts[cohort_key(series_key, version, bucket)] = tuple(
                -0.02 + index * 0.002 for index in range(20)
            )
    forecast = _forecast(history, ForecastResidualCalibration(cohorts))

    assert all(slot.energy.p10_kwh is not None for slot in forecast.load.slots)
    assert all(slot.energy.p10_kwh is not None for slot in forecast.pv.slots)
    assert all(
        slot.energy.p10_kwh is not None
        for component in forecast.load.components
        for slot in component.slots
    )
    for index, total in enumerate(forecast.load.slots):
        assert total.energy.p50_kwh == point.load.slots[index].energy.p50_kwh
        assert forecast.pv.slots[index].energy.p50_kwh == (
            point.pv.slots[index].energy.p50_kwh
        )
        assert total.energy.p50_kwh == sum(
            component.slots[index].energy.p50_kwh
            for component in forecast.load.components
        )

    # Quantiles do not authorize control and must not be folded into total P50.
    changed_load = replace(
        forecast.load,
        slots=tuple(
            replace(slot, energy=replace(slot.energy, p10_kwh=0.0, p90_kwh=9.0))
            for slot in forecast.load.slots
        ),
    )
    changed_pv = replace(
        forecast.pv,
        slots=tuple(
            replace(slot, energy=replace(slot.energy, p10_kwh=0.0, p90_kwh=9.0))
            for slot in forecast.pv.slots
        ),
    )
    constraints = BatteryConstraints(6.0, 5.0, 100.0, 2400.0, 2400.0, 0.8)

    def optimize(bundle):
        return DynamicProgrammingOptimizer().optimize(
            OptimizationProblem(
                "quantile-parity",
                bundle.load.generated_at_ms,
                bundle,
                tuple(MarketSlot(slot.slot, 30.0) for slot in bundle.load.slots),
                BatteryState(bundle.load.generated_at_ms, 50.0),
                constraints,
                CommercialPolicy(2.0),
            )
        )

    assert optimize(forecast) == optimize(
        replace(forecast, load=changed_load, pv=changed_pv)
    )


def test_calibration_state_is_immutable_during_forecast():
    as_of = dt.datetime(2026, 9, 15, 18, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    history = _history(as_of)
    state = ForecastLearningState(
        quantile_residuals={
            cohort_key(TOTAL_LOAD_SERIES, "component-load-v1", 0): [0.1] * 12
        }
    )
    before = dict(state.quantile_residuals)
    _forecast(history, calibration_from_state(state))
    assert state.quantile_residuals == before


def test_issued_forecast_survives_state_round_trip_and_matures(tmp_path):
    as_of = dt.datetime(2026, 9, 15, 18, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    history = _history(as_of)
    bundle = _forecast(history)
    state_path = tmp_path / "battery_strategy_optimizer_state.json"
    store = PlanningStateStore(str(state_path))
    owner = store.load(settings_from_values(), int(as_of.timestamp() * 1000))
    queue_predictions(owner.forecast, bundle)
    assert store.save(owner)

    restored = store.load(settings_from_values(), int(as_of.timestamp() * 1000))
    issued = restored.forecast.quantile_pending[0]
    target_ms = issued["target_ms"]
    p50 = issued["series"][TOTAL_LOAD_SERIES][1]
    target_actual = replace(
        history[-1],
        slot=SlotKey(target_ms, target_ms + SLOT_MS),
        house_load_no_ev_kwh=p50 + 0.125,
    )
    mature_predictions(
        restored.forecast,
        (target_actual,),
        as_of_ms=target_ms + SLOT_MS,
        timezone="Europe/Berlin",
    )
    calibration = calibration_from_state(restored.forecast)
    target_local = dt.datetime.fromtimestamp(target_ms / 1000, dt.UTC).astimezone(
        ZoneInfo("Europe/Berlin")
    )
    residuals = calibration.residuals_for(
        TOTAL_LOAD_SERIES,
        issued["series"][TOTAL_LOAD_SERIES][0],
        issued["generated_at_ms"],
        target_ms,
        target_local.weekday() >= 5,
        p50 > 1e-9,
    )

    # One sample is retained but intentionally remains below the publish gate.
    assert residuals == ()
    assert any(
        values == [0.125] for values in restored.forecast.quantile_residuals.values()
    )

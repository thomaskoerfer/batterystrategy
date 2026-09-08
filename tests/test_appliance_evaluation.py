"""Walk-forward evaluation tests for cyclic appliances."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from custom_components.battery_strategy.appliance_evaluation import (
    evaluate_cyclic_appliance,
)
from custom_components.battery_strategy.contracts import (
    HistoricalFeatureSlot,
    LoadComponentEnergy,
    LoadFeatureValue,
    SlotKey,
)

SLOT_MS = 900_000


def test_identical_cycles_have_zero_walk_forward_error():
    profile = (0.1, 0.2, 0.15, 0.05)
    energy = [0.0] * (7 * 96)
    for start in (20, 140, 260, 380, 500):
        energy[start : start + len(profile)] = profile
    history = tuple(
        HistoricalFeatureSlot(
            SlotKey(index * SLOT_MS, (index + 1) * SLOT_MS),
            0.2 + value,
            0.0,
            0.2 + value,
            0.0,
            0.0,
            0.0,
            0.0,
            30.0,
            load_components=(
                LoadComponentEnergy(
                    "dryer",
                    value,
                    features=(
                        LoadFeatureValue(
                            "cycle_active_fraction", 1.0 if value else 0.0
                        ),
                    ),
                ),
            ),
        )
        for index, value in enumerate(energy)
    )

    report = evaluate_cyclic_appliance(history, "dryer")

    assert report.completed_cycles == 5
    assert report.evaluated_cycles == 2
    assert report.remaining_energy_mae_kwh == 0.0
    assert report.slot_mae_kwh == 0.0
    assert report.end_slot_mae == 0.0
    assert report.context_remaining_energy_mae_kwh == 0.0
    assert report.context_slot_mae_kwh == 0.0
    assert report.context_end_slot_mae == 0.0


def test_walkforward_cli_runs_outside_repository(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "battery_strategy_appliance_walkforward.py"
    )

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--component-key" in result.stdout

"""Fast live-control primitives modeled after the Zendure HA manager."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from math import sqrt

from .const import COMMAND_IDLE, COMMAND_INPUT, COMMAND_OUTPUT
from .models import StrategyCommand

FAST_UPDATE_INTERVAL_S = 2.2
NORMAL_UPDATE_INTERVAL_S = 4.0
P1_STDDEV_FACTOR = 3.5
P1_STDDEV_MIN_W = 15.0
POWER_TOLERANCE_W = 5.0
POWER_START_W = 50
CHARGE_RESTART_FAST_S = 2.0
CHARGE_RESTART_SLOW_S = 60.0
CHARGE_RECENT_WINDOW_S = 300.0


class P1UpdateGate:
    """Rate-limit meter events while allowing significant changes through early."""

    def __init__(self) -> None:
        self._history: deque[int] = deque([25, -25], maxlen=8)
        self._next_normal_s = 0.0
        self._next_fast_s = 0.0

    def should_refresh(self, p1_w: float, now_s: float) -> bool:
        """Return whether a combined grid reading should run the live controller."""
        p1 = int(round(p1_w))
        if now_s < self._next_fast_s:
            self._history.append(p1)
            return False

        is_fast = False
        if len(self._history) > 1:
            average = int(sum(self._history) / len(self._history))
            stddev = P1_STDDEV_FACTOR * max(
                P1_STDDEV_MIN_W,
                sqrt(
                    sum((sample - average) ** 2 for sample in self._history)
                    / len(self._history)
                ),
            )
            is_fast = (
                abs(p1 - average) > stddev
                or abs(p1 - self._history[0]) > stddev
            )
            if is_fast:
                self._history.clear()
        self._history.append(p1)

        if not is_fast and now_s <= self._next_normal_s:
            return False
        self._next_normal_s = now_s + NORMAL_UPDATE_INTERVAL_S
        self._next_fast_s = now_s + FAST_UPDATE_INTERVAL_S
        return True


class DirectionHysteresis:
    """Prevent rapid discharge-to-charge oscillation like Zendure Manager."""

    def __init__(self) -> None:
        self._direction: str | None = None
        self._charge_block_until_s: float | None = None
        self._last_charge_s = float("-inf")

    def apply(
        self,
        command: StrategyCommand,
        measured_battery_power_w: float,
        now_s: float,
    ) -> StrategyCommand:
        """Return the command allowed by the direction-change guard."""
        if self._direction is None:
            if measured_battery_power_w > POWER_TOLERANCE_W:
                self._direction = COMMAND_OUTPUT
            elif measured_battery_power_w < -POWER_TOLERANCE_W:
                self._direction = COMMAND_INPUT
                self._last_charge_s = now_s

        if command.mode == COMMAND_OUTPUT:
            if (
                abs(measured_battery_power_w) <= POWER_TOLERANCE_W
                and command.power_w < POWER_START_W
            ):
                return replace(
                    command,
                    mode=COMMAND_IDLE,
                    power_w=0,
                    reason="power_start_threshold",
                )
            self._direction = COMMAND_OUTPUT
            self._charge_block_until_s = None
            return command

        if command.mode != COMMAND_INPUT:
            return command

        if self._charge_block_until_s is None and self._direction == COMMAND_OUTPUT:
            delay_s = (
                CHARGE_RESTART_FAST_S
                if now_s - self._last_charge_s > CHARGE_RECENT_WINDOW_S
                else CHARGE_RESTART_SLOW_S
            )
            self._charge_block_until_s = now_s + delay_s

        if (
            self._charge_block_until_s is not None
            and now_s < self._charge_block_until_s
        ):
            return replace(
                command,
                mode=COMMAND_IDLE,
                power_w=0,
                reason="direction_hysteresis",
            )

        if (
            abs(measured_battery_power_w) <= POWER_TOLERANCE_W
            and command.power_w < POWER_START_W
        ):
            return replace(
                command,
                mode=COMMAND_IDLE,
                power_w=0,
                reason="power_start_threshold",
            )

        self._charge_block_until_s = None
        self._direction = COMMAND_INPUT
        self._last_charge_s = now_s
        return command

"""Fast live-control primitives modeled after the Zendure HA manager."""

from __future__ import annotations

from collections import deque
from math import sqrt

FAST_UPDATE_INTERVAL_S = 2.2
NORMAL_UPDATE_INTERVAL_S = 4.0
P1_STDDEV_FACTOR = 3.5
P1_STDDEV_MIN_W = 15.0


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
            is_fast = abs(p1 - average) > stddev or abs(p1 - self._history[0]) > stddev
            if is_fast:
                self._history.clear()
        self._history.append(p1)

        if not is_fast and now_s <= self._next_normal_s:
            return False
        self._next_normal_s = now_s + NORMAL_UPDATE_INTERVAL_S
        self._next_fast_s = now_s + FAST_UPDATE_INTERVAL_S
        return True

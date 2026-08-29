"""Actuation guardrails for Battery Strategy."""

from __future__ import annotations

from dataclasses import dataclass

from .const import COMMAND_IDLE, COMMAND_INPUT, COMMAND_OUTPUT
from .models import StrategyCommand, StrategyOptions

MIN_WRITE_INTERVAL_S = 15.0
TRACKED_WRITE_MIN_INTERVAL_S = 2.2
CONFIRM_RETRY_INTERVAL_S = 8.0
POWER_TOLERANCE_W = 5.0


@dataclass(frozen=True)
class ZendureTargets:
    """Target mode and limits for Zendure actuation."""

    mode_option: str | None
    input_limit_w: int
    output_limit_w: int


@dataclass(frozen=True)
class PendingWrite:
    """One command awaiting confirmation from the device state."""

    target: float | str
    written_at_s: float


class ActuationWriteTracker:
    """Track our own writes instead of treating device reports as writes."""

    def __init__(self) -> None:
        self._last_write_s: dict[str, float] = {}
        self._pending: dict[str, PendingWrite] = {}

    def should_write_limit(
        self,
        key: str,
        current_w: float,
        target_w: float,
        now_s: float,
        options: StrategyOptions,
        *,
        force_zero: bool = False,
    ) -> bool:
        """Return whether a numeric target needs an initial write or retry."""
        tolerance_w = max(
            POWER_TOLERANCE_W, float(options.min_command_delta_w)
        )
        if abs(float(current_w) - float(target_w)) < tolerance_w:
            self._pending.pop(key, None)
            return False

        pending = self._pending.get(key)
        if pending is not None and abs(float(pending.target) - float(target_w)) < 0.5:
            return now_s - pending.written_at_s >= CONFIRM_RETRY_INTERVAL_S

        if force_zero and current_w > 0 and target_w == 0:
            return True
        last_write_s = self._last_write_s.get(key, float("-inf"))
        return now_s - last_write_s >= TRACKED_WRITE_MIN_INTERVAL_S

    def should_write_mode(
        self,
        key: str,
        current_mode: str,
        target_mode: str | None,
        now_s: float,
    ) -> bool:
        """Return whether a mode target needs an initial write or retry."""
        if not target_mode or _normalize_mode(current_mode) == _normalize_mode(
            target_mode
        ):
            self._pending.pop(key, None)
            return False
        pending = self._pending.get(key)
        if pending is not None and _normalize_mode(str(pending.target)) == _normalize_mode(
            target_mode
        ):
            return now_s - pending.written_at_s >= CONFIRM_RETRY_INTERVAL_S
        last_write_s = self._last_write_s.get(key, float("-inf"))
        return now_s - last_write_s >= TRACKED_WRITE_MIN_INTERVAL_S

    def record(self, key: str, target: float | str, now_s: float) -> None:
        """Record a command that must later be confirmed by reported state."""
        self._last_write_s[key] = now_s
        self._pending[key] = PendingWrite(target, now_s)

    def clear(self, key: str) -> None:
        """Forget pending state after a deliberate out-of-band safe stop."""
        self._pending.pop(key, None)


def zendure_targets(command: StrategyCommand) -> ZendureTargets:
    """Convert a strategy command into Zendure mode and limit targets."""
    if command.mode == COMMAND_INPUT:
        return ZendureTargets("Input mode", int(command.power_w), 0)
    if command.mode == COMMAND_OUTPUT:
        return ZendureTargets("Output mode", 0, int(command.power_w))
    if command.mode == COMMAND_IDLE:
        return ZendureTargets(None, 0, 0)
    return ZendureTargets(None, 0, 0)


def should_write_limit(
    current_w: float,
    target_w: float,
    last_changed_age_s: float,
    options: StrategyOptions,
    *,
    force_zero: bool = False,
) -> bool:
    """Return whether a limit write is worth sending."""
    if force_zero and current_w > 0 and target_w == 0:
        return True
    if last_changed_age_s < MIN_WRITE_INTERVAL_S:
        return False
    return abs(float(current_w) - float(target_w)) >= float(options.min_command_delta_w)


def should_write_mode(current_mode: str, target_mode: str | None, last_changed_age_s: float) -> bool:
    """Return whether a mode write is worth sending."""
    if not target_mode or _normalize_mode(current_mode) == _normalize_mode(target_mode):
        return False
    return last_changed_age_s >= MIN_WRITE_INTERVAL_S


def _normalize_mode(mode: str | None) -> str:
    """Normalize Zendure mode labels across state and service option formats."""
    return str(mode or "").lower().replace(" mode", "").strip()

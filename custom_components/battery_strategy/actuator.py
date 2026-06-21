"""Actuation guardrails for Battery Strategy."""

from __future__ import annotations

from dataclasses import dataclass

from .const import COMMAND_IDLE, COMMAND_INPUT, COMMAND_OUTPUT
from .models import StrategyCommand, StrategyOptions

MIN_WRITE_INTERVAL_S = 15.0


@dataclass(frozen=True)
class ZendureTargets:
    """Target mode and limits for Zendure actuation."""

    mode_option: str | None
    input_limit_w: int
    output_limit_w: int


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
    if last_changed_age_s < MIN_WRITE_INTERVAL_S:
        return False
    if force_zero and current_w > 0 and target_w == 0:
        return True
    return abs(float(current_w) - float(target_w)) >= float(options.min_command_delta_w)


def should_write_mode(current_mode: str, target_mode: str | None, last_changed_age_s: float) -> bool:
    """Return whether a mode write is worth sending."""
    if not target_mode or _normalize_mode(current_mode) == _normalize_mode(target_mode):
        return False
    return last_changed_age_s >= MIN_WRITE_INTERVAL_S


def _normalize_mode(mode: str | None) -> str:
    """Normalize Zendure mode labels across state and service option formats."""
    return str(mode or "").lower().replace(" mode", "").strip()

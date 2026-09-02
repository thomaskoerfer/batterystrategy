"""Actuation guardrails for Battery Strategy."""

from __future__ import annotations

from dataclasses import dataclass
import time

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
        tolerance_w = max(POWER_TOLERANCE_W, float(options.min_command_delta_w))
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
        if pending is not None and _normalize_mode(
            str(pending.target)
        ) == _normalize_mode(target_mode):
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


def should_write_mode(
    current_mode: str, target_mode: str | None, last_changed_age_s: float
) -> bool:
    """Return whether a mode write is worth sending."""
    if not target_mode or _normalize_mode(current_mode) == _normalize_mode(target_mode):
        return False
    return last_changed_age_s >= MIN_WRITE_INTERVAL_S


def _normalize_mode(mode: str | None) -> str:
    """Normalize Zendure mode labels across state and service option formats."""
    return str(mode or "").lower().replace(" mode", "").strip()


class HomeAssistantZendureActuator:
    """Sole Home Assistant service-writing boundary for Zendure controls."""

    def __init__(self, hass, ac_mode: str, input_limit: str, output_limit: str) -> None:
        self._hass = hass
        self._ac_mode = ac_mode
        self._input_limit = input_limit
        self._output_limit = output_limit
        self._tracker = ActuationWriteTracker()
        self._failsafe_reason: str | None = None

    def controls_available(self, *, include_mode: bool = True) -> bool:
        """Return whether all required control entities have usable states."""
        entities = [self._input_limit, self._output_limit]
        if include_mode:
            entities.insert(0, self._ac_mode)
        return all(self._available(entity) for entity in entities)

    async def zero(self, reason: str, *, blocking: bool, always_write: bool) -> dict:
        """Set both limits to zero through the single hardware-writing port."""
        if not self.controls_available(include_mode=False):
            return {"status": "skipped", "reason": "control_entity_unavailable"}
        current_input = self._float(self._input_limit)
        current_output = self._float(self._output_limit)
        actions: list[str] = []
        now_s = time.monotonic()
        for entity, current, label in (
            (self._input_limit, current_input, "input_limit"),
            (self._output_limit, current_output, "output_limit"),
        ):
            if always_write or current > 0:
                await self._write_limit(entity, 0, blocking, now_s)
                actions.append(f"{label}=0")
        return {
            "status": "disabled_zeroed"
            if reason == "strategy_disabled"
            else "failsafe_zeroed",
            "reason": reason,
            "actions": actions,
            "current_input_limit_w": current_input,
            "current_output_limit_w": current_output,
        }

    async def apply(self, command: StrategyCommand, options: StrategyOptions) -> dict:
        """Apply one validated strategy command with ordered vendor writes."""
        if not self.controls_available():
            return {"status": "skipped", "reason": "control_entity_unavailable"}
        self._failsafe_reason = None
        targets = zendure_targets(command)
        current_mode = self._hass.states.get(self._ac_mode).state
        current_input = self._float(self._input_limit)
        current_output = self._float(self._output_limit)
        actions: list[str] = []
        now_s = time.monotonic()

        async def write_limit(entity: str, value: int, label: str) -> None:
            await self._write_limit(entity, value, True, now_s)
            actions.append(f"{label}={value}")

        if command.mode == COMMAND_OUTPUT and self._tracker.should_write_limit(
            self._input_limit, current_input, 0, now_s, options, force_zero=True
        ):
            await write_limit(self._input_limit, 0, "input_limit")
            current_input = 0
        elif command.mode == COMMAND_INPUT and self._tracker.should_write_limit(
            self._output_limit, current_output, 0, now_s, options, force_zero=True
        ):
            await write_limit(self._output_limit, 0, "output_limit")
            current_output = 0

        if command.mode != COMMAND_IDLE and self._tracker.should_write_mode(
            self._ac_mode, current_mode, targets.mode_option, now_s
        ):
            await self._hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": self._ac_mode, "option": targets.mode_option},
                blocking=True,
            )
            self._tracker.record(self._ac_mode, targets.mode_option or "", now_s)
            actions.append(f"mode={targets.mode_option}")

        if self._tracker.should_write_limit(
            self._input_limit,
            current_input,
            targets.input_limit_w,
            now_s,
            options,
            force_zero=True,
        ):
            await write_limit(self._input_limit, targets.input_limit_w, "input_limit")
        if self._tracker.should_write_limit(
            self._output_limit,
            current_output,
            targets.output_limit_w,
            now_s,
            options,
            force_zero=True,
        ):
            await write_limit(
                self._output_limit, targets.output_limit_w, "output_limit"
            )

        return {
            "status": "written" if actions else "no_change",
            "actions": actions,
            "target_mode": targets.mode_option,
            "target_input_limit_w": targets.input_limit_w,
            "target_output_limit_w": targets.output_limit_w,
            "current_mode": current_mode,
            "current_input_limit_w": current_input,
            "current_output_limit_w": current_output,
        }

    async def failsafe_zero_once(self, reason: str) -> dict:
        """Issue one safe stop for a persistent invalid-input reason."""
        if self._failsafe_reason == reason:
            return {"status": "failsafe_no_write", "reason": reason}
        result = await self.zero(reason, blocking=True, always_write=False)
        if result.get("status") != "skipped":
            self._failsafe_reason = reason
        return result

    async def _write_limit(
        self, entity: str, value: int, blocking: bool, now_s: float
    ) -> None:
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity, "value": value},
            blocking=blocking,
        )
        self._tracker.record(entity, float(value), now_s)

    def _available(self, entity: str) -> bool:
        state = self._hass.states.get(entity)
        return state is not None and state.state not in (
            "unknown",
            "unavailable",
            "none",
            "",
        )

    def _float(self, entity: str) -> float:
        try:
            return float(self._hass.states.get(entity).state)
        except (AttributeError, TypeError, ValueError):
            return 0.0

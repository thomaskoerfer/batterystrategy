"""Single hardware-writing adapter for Battery Strategy."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .config_definitions import option_default
from .contracts import ActuationResult, BatteryCommand, CommandMode

TRACKED_WRITE_MIN_INTERVAL_S = 2.2
CONFIRM_RETRY_INTERVAL_S = 8.0
POWER_TOLERANCE_W = 5.0
_FAILSAFE_REASONS = frozenset(
    {
        "battery_power_unavailable",
        "battery_soc_unavailable",
        "grid_inputs_stale",
        "ev_power_unavailable",
    }
)


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
        min_command_delta_w: float,
        *,
        force_zero: bool = False,
    ) -> bool:
        """Return whether a numeric target needs an initial write or retry."""
        tolerance_w = max(POWER_TOLERANCE_W, float(min_command_delta_w))
        pending = self._pending.get(key)
        if pending is not None and abs(float(pending.target) - float(target_w)) < 0.5:
            if abs(float(current_w) - float(target_w)) < tolerance_w:
                self._pending.pop(key, None)
                return False
            return now_s - pending.written_at_s >= CONFIRM_RETRY_INTERVAL_S

        # Direction changes and safety stops must clear any positive opposite
        # limit even below the normal deadband, then await confirmation/retry.
        if force_zero and current_w > 0 and target_w == 0:
            return True
        if abs(float(current_w) - float(target_w)) < tolerance_w:
            self._pending.pop(key, None)
            return False

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


def zendure_targets(command: BatteryCommand) -> ZendureTargets:
    """Convert a generic command into Zendure mode and limit targets."""
    if command.mode == CommandMode.INPUT:
        return ZendureTargets("Input mode", int(command.power_w), 0)
    if command.mode == CommandMode.OUTPUT:
        return ZendureTargets("Output mode", 0, int(command.power_w))
    return ZendureTargets(None, 0, 0)


def _normalize_mode(mode: str | None) -> str:
    """Normalize Zendure mode labels across state and service option formats."""
    return str(mode or "").lower().replace(" mode", "").strip()


class HomeAssistantZendureActuator:
    """Zendure adapter satisfying the generic BatteryActuator interface."""

    def __init__(
        self,
        hass,
        ac_mode: str,
        input_limit: str,
        output_limit: str,
        *,
        min_command_delta_w: Callable[[], float] | None = None,
    ) -> None:
        self._hass = hass
        self._ac_mode = ac_mode
        self._input_limit = input_limit
        self._output_limit = output_limit
        self._min_command_delta_w = min_command_delta_w or (
            lambda: float(option_default("min_command_delta_w"))
        )
        self._tracker = ActuationWriteTracker()

    def controls_available(self, *, include_mode: bool = True) -> bool:
        """Return whether all required control entities have usable states."""
        entities = [self._input_limit, self._output_limit]
        if include_mode:
            entities.insert(0, self._ac_mode)
        return all(self._available(entity) for entity in entities)

    def limits_zero_confirmed(self) -> bool:
        """Return whether both device-reported direction limits are safely zero."""
        return bool(
            self.controls_available(include_mode=False)
            and abs(self._float(self._input_limit)) <= POWER_TOLERANCE_W
            and abs(self._float(self._output_limit)) <= POWER_TOLERANCE_W
        )

    async def apply(self, command: BatteryCommand) -> ActuationResult:
        """Apply one validated generic command with ordered vendor writes."""
        applied_at_ms = int(time.time() * 1000)
        if applied_at_ms >= command.valid_until_ms:
            return ActuationResult(command.command_id, False, applied_at_ms, "expired")

        failsafe = command.reason in _FAILSAFE_REASONS
        if not self.controls_available(include_mode=command.mode != CommandMode.IDLE):
            return ActuationResult(
                command.command_id,
                False,
                applied_at_ms,
                "control_entity_unavailable",
            )

        targets = zendure_targets(command)
        current_mode = self._state(self._ac_mode)
        current_input = self._float(self._input_limit)
        current_output = self._float(self._output_limit)
        actions: list[str] = []
        now_s = time.monotonic()
        min_delta_w = max(0.0, float(self._min_command_delta_w()))
        force_disabled_zero = (
            command.mode == CommandMode.IDLE and command.reason == "strategy_disabled"
        )

        async def write_limit(entity: str, value: int, label: str) -> None:
            await self._write_limit(entity, value, now_s)
            actions.append(f"{label}={value}")

        if command.mode == CommandMode.OUTPUT and self._tracker.should_write_limit(
            self._input_limit,
            current_input,
            0,
            now_s,
            min_delta_w,
            force_zero=True,
        ):
            await write_limit(self._input_limit, 0, "input_limit")
            current_input = 0
        elif command.mode == CommandMode.INPUT and self._tracker.should_write_limit(
            self._output_limit,
            current_output,
            0,
            now_s,
            min_delta_w,
            force_zero=True,
        ):
            await write_limit(self._output_limit, 0, "output_limit")
            current_output = 0

        if command.mode != CommandMode.IDLE and self._tracker.should_write_mode(
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

        for entity, current, target, label in (
            (
                self._input_limit,
                current_input,
                targets.input_limit_w,
                "input_limit",
            ),
            (
                self._output_limit,
                current_output,
                targets.output_limit_w,
                "output_limit",
            ),
        ):
            should_write = force_disabled_zero or self._tracker.should_write_limit(
                entity,
                current,
                target,
                now_s,
                min_delta_w,
                force_zero=True,
            )
            if should_write:
                await write_limit(entity, target, label)

        if failsafe and not actions:
            status = (
                "failsafe_confirmed"
                if current_input == 0 and current_output == 0
                else "failsafe_pending_confirmation"
            )
        else:
            status = "written" if actions else "no_change"
        detail = status if not actions else f"{status}:{','.join(actions)}"
        return ActuationResult(command.command_id, bool(actions), applied_at_ms, detail)

    async def _write_limit(self, entity: str, value: int, now_s: float) -> None:
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity, "value": value},
            blocking=True,
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

    def _state(self, entity: str) -> str:
        state = self._hass.states.get(entity)
        return str(state.state) if state is not None else ""

    def _float(self, entity: str) -> float:
        try:
            return float(self._hass.states.get(entity).state)
        except AttributeError, TypeError, ValueError:
            return 0.0

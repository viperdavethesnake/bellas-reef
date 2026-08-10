"""Local interlock enforcement.

This module is the reason `hardware-io` exists as a separate service. Everything
here must keep working when the control engine is dead, the spine is down and
Postgres is unreachable — so nothing in this file may await anything
network-bound.

Two independent protections, with deliberately different recovery semantics:

**Heartbeat loss** drives the actuator to its safe state and does *not* latch.
The controller being briefly gone is a transient; when it returns, the actuator
stays safe until something explicitly commands it again. It never springs back
on by itself.

**Max continuous runtime** drives to safe state *and latches*. A pump that has
run past its cap means a sensor or a control loop is lying, and the only correct
response is to stop and stay stopped until a human looks. Clearing the latch is
an explicit operator action (PRD R2).

Timing is deadline-driven, never polled. A poll loop would mean safe state
arrives up to one poll interval after the declared timeout, which would make
`heartbeat_timeout_s` a lower bound rather than the guarantee it is supposed to
be.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from bellasreef_contracts import ActuatorCommand, ActuatorLevel, ActuatorRegistration
from bellasreef_contracts.driver import ActuatorDriver

__all__ = [
    "CommandOutcome",
    "InterlockSupervisor",
    "SafetyEvent",
    "TripReason",
]

TripReason = Literal[
    "heartbeat_timeout",
    "max_runtime_exceeded",
    "command_expired",
    "latched",
    "clock_untrusted",
    "shutdown",
]

CommandOutcome = Literal["applied", "rejected_expired", "rejected_latched", "rejected_clock"]

NowFn = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SafetyEvent:
    """Something the audit log must hear about."""

    actuator_id: str
    reason: TripReason
    at: datetime
    detail: str


EventSink = Callable[[SafetyEvent], Awaitable[None]]


@dataclass(slots=True)
class _Guard:
    registration: ActuatorRegistration
    driver: ActuatorDriver

    #: Set when a beat arrives, so the watcher wakes immediately rather than
    #: waiting out a stale deadline.
    beat: asyncio.Event = field(default_factory=asyncio.Event)
    last_beat: float = 0.0

    latched: bool = False
    latch_detail: str = ""
    heartbeat_lost: bool = False

    at_safe_state: bool = True
    runtime_task: asyncio.Task[None] | None = None
    watch_task: asyncio.Task[None] | None = None

    @property
    def actuator_id(self) -> str:
        return self.registration.actuator_id


class InterlockSupervisor:
    """Owns every actuator's failure behaviour.

    ``clock_trusted`` reflects whether the host clock is synchronised. This host
    has no RTC battery, so after a power cut the clock is wrong until chrony
    catches up. Command expiry is meaningless against a wrong clock, so commands
    are refused rather than guessed at.
    """

    def __init__(
        self,
        *,
        on_event: EventSink,
        now: NowFn = _utcnow,
        clock_trusted: bool = True,
    ) -> None:
        self._guards: dict[str, _Guard] = {}
        self._on_event = on_event
        self._now = now
        self._clock_trusted = clock_trusted
        self._running = False

    # ------------------------------------------------------------- lifecycle

    def register(self, registration: ActuatorRegistration, driver: ActuatorDriver) -> None:
        """Register an actuator.

        The model already guarantees ``safe_state``, ``max_runtime_s`` and
        ``heartbeat_timeout_s`` are present — an unregisterable actuator cannot
        be constructed — so there is nothing to re-validate here.
        """
        if registration.actuator_id in self._guards:
            raise ValueError(f"actuator already registered: {registration.actuator_id}")
        self._guards[registration.actuator_id] = _Guard(registration=registration, driver=driver)

    async def start(self) -> None:
        """Begin watching. Every actuator is driven to its safe state first.

        Startup asserts safe state rather than assuming it — hardware may have
        come up in any state, and a relay board that survived a power cut
        energised is exactly what this catches.
        """
        self._running = True
        loop = asyncio.get_running_loop()
        failures: list[Exception] = []

        for guard in self._guards.values():
            guard.last_beat = loop.time()
            try:
                await guard.driver.drive_safe()
                guard.at_safe_state = True
            except Exception as exc:
                # An actuator we could not prove safe is not one we will command.
                guard.latched = True
                guard.latch_detail = f"failed to reach safe state at startup: {exc!r}"
                await self._emit(guard, "shutdown", guard.latch_detail)
                failures.append(exc)
            guard.watch_task = asyncio.create_task(
                self._watch_heartbeat(guard), name=f"hb-{guard.actuator_id}"
            )

        if failures:
            raise ExceptionGroup("actuators failed to reach safe state at startup", failures)

    async def stop(self) -> None:
        """Shut down. Every actuator goes safe on the way out.

        Each driver is isolated. One that raises must not strand the actuators
        after it in the loop — a single bad device turning a clean shutdown into
        a tank full of running equipment is precisely the failure this service
        exists to prevent. Failures are collected and re-raised together, after
        every other actuator has had its chance.
        """
        self._running = False
        failures: list[Exception] = []

        for guard in self._guards.values():
            for task in (guard.watch_task, guard.runtime_task):
                if task is not None and not task.done():
                    task.cancel()
            guard.watch_task = None
            guard.runtime_task = None
            try:
                await self._drive_safe(guard, "shutdown", "supervisor stopping")
            except Exception as exc:
                await self._emit(guard, "shutdown", f"drive_safe FAILED during shutdown: {exc!r}")
                failures.append(exc)

        if failures:
            raise ExceptionGroup("actuators failed to reach safe state on shutdown", failures)

    # ------------------------------------------------------------- heartbeat

    def heartbeat(self) -> None:
        """Record a beat from the control engine.

        Cheap and synchronous on purpose: it is called on every beat and must
        never be a place where back-pressure builds.
        """
        loop = asyncio.get_running_loop()
        stamp = loop.time()
        for guard in self._guards.values():
            guard.last_beat = stamp
            guard.beat.set()

    async def _watch_heartbeat(self, guard: _Guard) -> None:
        loop = asyncio.get_running_loop()
        timeout = guard.registration.heartbeat_timeout_s
        while self._running:
            remaining = (guard.last_beat + timeout) - loop.time()
            if remaining <= 0:
                if not guard.heartbeat_lost:
                    guard.heartbeat_lost = True
                    await self._drive_safe(
                        guard,
                        "heartbeat_timeout",
                        f"no heartbeat for {timeout}s",
                    )
                # Sleep until a beat arrives; do not spin.
                await guard.beat.wait()
                guard.beat.clear()
                guard.heartbeat_lost = False
                continue
            try:
                await asyncio.wait_for(guard.beat.wait(), timeout=remaining)
            except TimeoutError:
                continue  # deadline reached; loop re-evaluates and trips
            guard.beat.clear()

    # ------------------------------------------------------------- commands

    async def apply(self, command: ActuatorCommand) -> CommandOutcome:
        """Apply a command, subject to every interlock.

        Order matters. Clock trust is checked before expiry, because an expiry
        decision made against an untrusted clock is not a decision.
        """
        guard = self._guards[command.actuator_id]

        if not self._clock_trusted:
            await self._emit(guard, "clock_untrusted", "clock not synchronised; command refused")
            return "rejected_clock"

        if command.is_expired(self._now()):
            await self._emit(
                guard, "command_expired", f"expired at {command.expires_at.isoformat()}"
            )
            return "rejected_expired"

        if guard.latched:
            await self._emit(guard, "latched", f"latched: {guard.latch_detail}")
            return "rejected_latched"

        await guard.driver.apply(command.level)
        self._note_level(guard, command.level)
        return "applied"

    def _note_level(self, guard: _Guard, level: ActuatorLevel) -> None:
        """Start or cancel the continuous-runtime timer."""
        is_safe = level == guard.registration.safe_state

        if is_safe:
            guard.at_safe_state = True
            if guard.runtime_task is not None and not guard.runtime_task.done():
                guard.runtime_task.cancel()
            guard.runtime_task = None
            return

        if guard.at_safe_state or guard.runtime_task is None or guard.runtime_task.done():
            # Transitioning out of safe state starts a fresh cap. Re-commanding
            # the same non-safe level does NOT extend it — otherwise a control
            # loop repeating itself would defeat the cap entirely.
            guard.at_safe_state = False
            if guard.runtime_task is None or guard.runtime_task.done():
                guard.runtime_task = asyncio.create_task(
                    self._runtime_deadline(guard), name=f"rt-{guard.actuator_id}"
                )

    async def _runtime_deadline(self, guard: _Guard) -> None:
        await asyncio.sleep(guard.registration.max_runtime_s)
        guard.latched = True
        guard.latch_detail = (
            f"ran continuously past max_runtime_s={guard.registration.max_runtime_s}"
        )
        await self._drive_safe(guard, "max_runtime_exceeded", guard.latch_detail)

    async def clear_latch(self, actuator_id: str, *, operator: str) -> None:
        """Explicit operator action. There is no automatic path out of a latch."""
        guard = self._guards[actuator_id]
        guard.latched = False
        guard.latch_detail = ""
        await self._emit(guard, "latched", f"latch cleared by {operator}")

    # ------------------------------------------------------------- internals

    async def _drive_safe(self, guard: _Guard, reason: TripReason, detail: str) -> None:
        await guard.driver.drive_safe()
        guard.at_safe_state = True
        if guard.runtime_task is not None and not guard.runtime_task.done():
            guard.runtime_task.cancel()
            guard.runtime_task = None
        await self._emit(guard, reason, detail)

    async def _emit(self, guard: _Guard, reason: TripReason, detail: str) -> None:
        await self._on_event(
            SafetyEvent(
                actuator_id=guard.actuator_id,
                reason=reason,
                at=self._now(),
                detail=detail,
            )
        )

    # ------------------------------------------------------------- inspection

    def registration_of(self, actuator_id: str) -> ActuatorRegistration:
        return self._guards[actuator_id].registration

    def is_latched(self, actuator_id: str) -> bool:
        return self._guards[actuator_id].latched

    @property
    def actuator_ids(self) -> tuple[str, ...]:
        return tuple(self._guards)

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
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
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

from bellasreef_contracts import ActuatorCommand, ActuatorLevel, ActuatorRegistration
from bellasreef_contracts.driver import ActuatorDriver
from bellasreef_service import get_logger

__all__ = [
    "CommandOutcome",
    "InterlockSupervisor",
    "SafetyEvent",
    "TripReason",
]

log = get_logger(__name__)

#: Seconds between safe-drive retries after a driver failure mid-trip. Module
#: level so tests can shorten it (see test_drills.py's retry drills).
RETRY_BACKOFF_S: Final = 1.0

TripReason = Literal[
    "heartbeat_timeout",
    "max_runtime_exceeded",
    "command_expired",
    "latched",
    "clock_untrusted",
    "shutdown",
    # Not a trip: nothing was moved and nothing latched. It is here because the
    # audit trail is the only place a misrouted controller becomes visible, and
    # that trail is keyed on this type.
    "unknown_actuator",
]

CommandOutcome = Literal[
    "applied",
    "rejected_expired",
    "rejected_latched",
    "rejected_clock",
    "rejected_unknown",
]

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

    #: Whether the actuator actually reached (or already was in) a safe state
    #: as part of this event. True by default: a refusal (latched,
    #: command_expired, clock_untrusted, unknown_actuator) never moved anything
    #: and reports it. A trip (heartbeat_timeout, max_runtime_exceeded) or the
    #: shutdown path goes through ``_drive_safe`` or, since 2026-08-23,
    #: ``_drive_safe_with_retry`` — a failed attempt there emits this same
    #: reason with ``reached_safe=False`` (retrying) before a later attempt, or
    #: the original synchronous ``_drive_safe`` call in ``start()``/``stop()``,
    #: succeeds with ``True``. A consumer that publishes a "safe" state from an
    #: event must check this first, or it claims a transition that never
    #: happened — including, now, on a reason it used to be able to trust
    #: unconditionally.
    reached_safe: bool = True


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

    @property
    def heartbeat_timeout_s(self) -> float:
        """Non-optional by construction — see `InterlockSupervisor.register`.

        The registration models these as optional because an advisory device
        has none. A guard only ever exists for an authoritative one, which the
        contract validator guarantees is complete.
        """
        timeout = self.registration.heartbeat_timeout_s
        assert timeout is not None
        return timeout

    @property
    def max_runtime_s(self) -> float:
        runtime = self.registration.max_runtime_s
        assert runtime is not None
        return runtime

    @property
    def safe_state(self) -> ActuatorLevel:
        level = self.registration.safe_state
        assert level is not None
        return level

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
        """Register an actuator. Authoritative devices only.

        docs/device-classes.md §3: this service is the one that is allowed to
        drive a heater, and it takes only devices we can actually command. A
        non-authoritative registration is refused here rather than tolerated,
        because everything below this line — the runtime cap, the heartbeat
        deadline, the drive-to-safe-state on lapse — assumes a guarantee that an
        advisory device never offered.

        Refusing it also makes the narrowing below sound: the contract validator
        guarantees an authoritative registration carries the complete safety
        triple, so these values are never ``None`` past this point.
        """
        if registration.actuator_id in self._guards:
            raise ValueError(f"actuator already registered: {registration.actuator_id}")
        if registration.control_authority != "authoritative":
            raise ValueError(
                f"hardware-io registers authoritative actuators only; "
                f"{registration.actuator_id!r} is {registration.control_authority!r}"
            )
        if (
            registration.safe_state is None
            or registration.max_runtime_s is None
            or registration.heartbeat_timeout_s is None
        ):  # pragma: no cover - unreachable while the contract validator holds
            raise ValueError(
                f"{registration.actuator_id!r} is authoritative but incomplete; "
                "the contract validator should have refused it"
            )
        self._guards[registration.actuator_id] = _Guard(registration=registration, driver=driver)
        guard = self._guards[registration.actuator_id]
        if self._running:
            # app.py registers production actuators from the registry AFTER
            # start() has run (the spine has to be up before the registry can
            # be read). A guard created then must get the same watcher a
            # start()-time guard gets, or heartbeat loss protects nothing —
            # which is exactly what shipped until 2026-08-23.
            loop = asyncio.get_running_loop()
            guard.last_beat = loop.time()
            guard.watch_task = asyncio.create_task(
                self._watch_heartbeat(guard), name=f"hb-{guard.actuator_id}"
            )

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
                await self._emit(guard, "shutdown", guard.latch_detail, reached_safe=False)
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
            tasks = [t for t in (guard.watch_task, guard.runtime_task) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            guard.watch_task = None
            guard.runtime_task = None
            if tasks:
                # A cancelled task can still be inside guard.driver.drive_safe()
                # — retries in _drive_safe_with_retry really sleep and retry
                # now, so cancellation does not land instantly. Wait for both
                # tasks to actually unwind before this loop drives the same
                # guard itself, or the two calls race on a bus ActuatorDriver
                # makes no reentrancy guarantee about (2026-08-23 review).
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self._drive_safe(guard, "shutdown", "supervisor stopping")
            except Exception as exc:
                await self._emit(
                    guard,
                    "shutdown",
                    f"drive_safe FAILED during shutdown: {exc!r}",
                    reached_safe=False,
                )
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
        timeout = guard.heartbeat_timeout_s
        while self._running:
            try:
                remaining = (guard.last_beat + timeout) - loop.time()
                if remaining <= 0:
                    if not guard.heartbeat_lost:
                        guard.heartbeat_lost = True
                        await self._drive_safe_with_retry(
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
            except asyncio.CancelledError:
                raise
            except Exception:
                # _drive_safe_with_retry already retries a failing driver
                # forever, so reaching here means something else went wrong in
                # the loop itself. The watcher must not die to it: a dead
                # watch task leaves heartbeat loss protecting nothing until
                # the process restarts (2026-08-23 finding 2, same failure
                # shape as the retry gap this method now closes).
                log.exception(
                    "heartbeat watcher hit an unexpected error; continuing",
                    extra={"actuator_id": guard.actuator_id},
                )
                await asyncio.sleep(RETRY_BACKOFF_S)

    # ------------------------------------------------------------- commands

    async def apply(self, command: ActuatorCommand) -> CommandOutcome:
        """Apply a command, subject to every interlock.

        Order matters. Clock trust is checked before expiry, because an expiry
        decision made against an untrusted clock is not a decision.

        A command naming an actuator we do not own is refused rather than
        raised. The distinction is not academic: this used to be a KeyError
        that escaped `apply`, escaped `drain_once`, and killed the process —
        and because BR_CMD is a workqueue, the same message was waiting on
        restart, so the service crash-looped and the probe went silent for the
        duration. A hub also has to tolerate this by design, since a phase-2
        spoke announcing actuators this node has never heard of is the normal
        case, not an error.
        """
        guard = self._guards.get(command.actuator_id)
        if guard is None:
            await self._emit_unowned(
                command.actuator_id,
                "unknown_actuator",
                "no such actuator on this node; command refused",
            )
            return "rejected_unknown"

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
        is_safe = level == guard.safe_state

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
        await asyncio.sleep(guard.max_runtime_s)
        guard.latched = True
        guard.latch_detail = (
            f"ran continuously past max_runtime_s={guard.registration.max_runtime_s}"
        )
        # Latching happens before the drive, not after: refusing commands
        # while a retry is in flight is correct, and a latch that waited for
        # drive_safe to succeed would leave the actuator commandable for
        # however long a flaky driver kept failing.
        await self._drive_safe_with_retry(guard, "max_runtime_exceeded", guard.latch_detail)

    async def clear_latch(self, actuator_id: str, *, operator: str) -> None:
        """Explicit operator action. There is no automatic path out of a latch."""
        guard = self._guards[actuator_id]
        guard.latched = False
        guard.latch_detail = ""
        await self._emit(guard, "latched", f"latch cleared by {operator}")

    # ------------------------------------------------------------- internals

    def _mark_safe(self, guard: _Guard) -> None:
        """Bookkeeping for a drive_safe() call that just succeeded.

        Split out of `_drive_safe` so `_drive_safe_with_retry` can apply it
        before its own success emit — see that method for why the emit must
        not be allowed to feed back into the retry decision.
        """
        guard.at_safe_state = True
        if guard.runtime_task is not None and not guard.runtime_task.done():
            guard.runtime_task.cancel()
            guard.runtime_task = None

    async def _drive_safe(self, guard: _Guard, reason: TripReason, detail: str) -> None:
        await guard.driver.drive_safe()
        self._mark_safe(guard)
        await self._emit(guard, reason, detail)

    async def _drive_safe_with_retry(self, guard: _Guard, reason: TripReason, detail: str) -> None:
        """Drive safe, retrying until the drive itself lands or the supervisor stops.

        Only `guard.driver.drive_safe()` decides whether to retry. The success
        path does its own bookkeeping and emit here, rather than delegating to
        `_drive_safe`, because that emit runs *after* the drive already
        succeeded — a sink that raises there is not a drive failure, and
        folding it into this method's own try/except would misreport it as
        one (spuriously re-driving an already-safe actuator and auditing a
        "drive_safe FAILED" that never happened — 2026-08-23 review).

        A trip is the one moment this service exists for; a transient driver
        error there must surface as an event and a retry, never as a dead task
        (2026-08-23 finding 2: latched-but-energised with nobody watching).
        """
        while True:
            try:
                await guard.driver.drive_safe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The event sink failing must not break the retry loop that is
                # the actual safety mechanism — app.py already guards its own
                # sink top-to-bottom, but this one does not depend on that.
                with contextlib.suppress(Exception):
                    await self._emit(
                        guard,
                        reason,
                        f"drive_safe FAILED during trip, retrying: {exc!r}",
                        reached_safe=False,
                    )
                if not self._running:
                    return
                await asyncio.sleep(RETRY_BACKOFF_S)
                continue
            self._mark_safe(guard)
            with contextlib.suppress(Exception):
                await self._emit(guard, reason, detail)
            return

    async def _emit(
        self, guard: _Guard, reason: TripReason, detail: str, *, reached_safe: bool = True
    ) -> None:
        await self._on_event(
            SafetyEvent(
                actuator_id=guard.actuator_id,
                reason=reason,
                at=self._now(),
                detail=detail,
                reached_safe=reached_safe,
            )
        )

    async def _emit_unowned(self, actuator_id: str, reason: TripReason, detail: str) -> None:
        """Report on an actuator that has no guard, because it has no guard.

        Separate from :meth:`_emit` only because that one reads its id off the
        guard, and here the whole point is that there isn't one. The event
        still has to go out: a controller commanding the wrong node is a
        misconfiguration that stays invisible if the message is silently
        dropped.
        """
        await self._on_event(
            SafetyEvent(
                actuator_id=actuator_id,
                reason=reason,
                at=self._now(),
                detail=detail,
            )
        )

    # ------------------------------------------------------------- inspection

    def registration_of(self, actuator_id: str) -> ActuatorRegistration:
        return self._guards[actuator_id].registration

    def driver_of(self, actuator_id: str) -> ActuatorDriver:
        """Read-only access to a registered driver.

        Broker-free, like every other accessor here — it exists so a spine-
        holding caller (CommandConsumer) can ask a driver what it actually did
        (``read_back()``) without safety.py taking any dependency on NATS."""
        return self._guards[actuator_id].driver

    def is_latched(self, actuator_id: str) -> bool:
        return self._guards[actuator_id].latched

    @property
    def actuator_ids(self) -> tuple[str, ...]:
        return tuple(self._guards)

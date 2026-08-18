# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""ActuatorState publishing: on every applied command, and once per actuator
at startup.

Before this, Spine.publish_state() had zero production callers — the hub never
told the wire what any actuator's level was, so every client showed "no state
yet" forever. These tests exercise the production call sites directly, against
a fake in-memory spine, so they run with no NATS at all.

Both call sites must survive a spine that raises (or is simply absent):
publish failures are a logged-and-continue concern, never a reason to fail a
command or crash startup — matching how ``_publish_reading`` already treats a
failed sensor publish.

The applied-command path additionally must publish what the driver actually
did, not what was asked for: a PWM driver's own snap_duty rule (dimming.py)
can turn a low-duty command into a dark pin, and the truth line exists
precisely to catch that divergence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from bellasreef_contracts import (
    ActuatorClass,
    ActuatorCommand,
    ActuatorLevel,
    ActuatorRegistration,
    ActuatorState,
    BinaryLevel,
    PwmLevel,
)
from bellasreef_hardware_io import FakeActuator, InterlockSupervisor, SafetyEvent
from bellasreef_hardware_io.app import HardwareIO
from bellasreef_hardware_io.drivers.dimming import snap_duty
from bellasreef_hardware_io.spine import CommandConsumer

OFF = BinaryLevel(on=False)
ON = BinaryLevel(on=True)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class FakeSpinePublisher:
    """Records every ActuatorState handed to publish_state.

    Duck-typed against Spine's async publish surface — only the one method
    either call site actually uses. ``raises`` lets a test prove a broken
    spine cannot break actuation or startup; ``attempts`` counts every call
    even when it raises, so a test can prove a publish was *tried* without
    depending on it having recorded anything.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.states: list[ActuatorState] = []
        self.raises = raises
        self.attempts = 0

    async def publish_state(self, state: ActuatorState) -> None:
        self.attempts += 1
        if self.raises:
            raise RuntimeError("spine unreachable")
        self.states.append(state)


class NoReadBackActuator(FakeActuator):
    """Mirrors the PCA9685 driver: honest ``None``, deliberately.

    The registers read back what was written, which confirms the I²C write
    landed and says nothing about whether the LED driver did anything with
    it — so pca9685.py's real ``read_back()`` always returns ``None`` rather
    than echo a confident-looking value. This fake matches that shape without
    touching I²C.
    """

    async def read_back(self) -> ActuatorLevel | None:
        return None


class RaisingReadBackActuator(FakeActuator):
    """A driver whose read_back() itself fails — the bus is gone, say."""

    async def read_back(self) -> ActuatorLevel | None:
        raise OSError("bus unavailable")


class SnappingFakeActuator:
    """A minimal PWM-class driver satisfying ``ActuatorDriver`` by hand.

    Mirrors pipwm.py's real round-trip: ``apply()`` snaps duty the same way
    ``duty_to_ns`` does (dimming.py's ``snap_duty`` — under 8% snaps to 0),
    and ``read_back()`` reports what was actually written, the way the real
    driver reads its own ``duty_cycle`` sysfs node back rather than trusting
    what it was told to write. No sysfs involved; this is the shape of the
    bug, not the hardware.
    """

    def __init__(self, actuator_id: str, safe_state: PwmLevel) -> None:
        self._actuator_id = actuator_id
        self._safe_state = safe_state
        self._level = safe_state

    @property
    def driver_id(self) -> str:
        return "fake-pwm"

    @property
    def actuator_id(self) -> str:
        return self._actuator_id

    @property
    def safe_state(self) -> ActuatorLevel:
        return self._safe_state

    async def open(self) -> None:
        pass

    async def apply(self, level: ActuatorLevel) -> None:
        assert isinstance(level, PwmLevel)
        self._level = PwmLevel(duty=snap_duty(level.duty))

    async def drive_safe(self) -> None:
        self._level = self._safe_state

    async def read_back(self) -> ActuatorLevel | None:
        return self._level


def registration(
    actuator_id: str,
    *,
    safe_state: ActuatorLevel = OFF,
    actuator_class: ActuatorClass = "binary",
) -> ActuatorRegistration:
    return ActuatorRegistration(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=actuator_id,
        actuator_class=actuator_class,
        role="outlet",
        driver_id="fake-actuator",
        control_authority="authoritative",
        failsafe_capable=True,
        transport="local",
        safe_state=safe_state,
        max_runtime_s=3600.0,
        heartbeat_timeout_s=30.0,
    )


def command(actuator_id: str, level: ActuatorLevel = ON) -> ActuatorCommand:
    now = datetime.now(UTC)
    return ActuatorCommand(
        message_id=uuid4(),
        emitted_at=now,
        source="control-engine",
        actuator_id=actuator_id,
        actuator_class=level.kind,
        level=level,
        idempotency_key=uuid4(),
        expires_at=now + timedelta(seconds=60),
    )


async def _swallow(event: SafetyEvent) -> None:
    return None


# --------------------------------------------------------- applied commands


def test_applied_command_publishes_the_snapped_level_not_the_commanded_one() -> None:
    """C1, the headline behaviour: the truth line reports what the hardware
    did, not what was asked for.

    A 5% duty command snaps to dark under dimming.py's rule (anything under
    8% snaps to 0 — session-4 ruling). Publishing ``command.level``
    unconditionally would report 5% on the wire and in VictoriaMetrics while
    the pin sat at 0%; publishing ``driver.read_back()`` instead reports what
    actually happened.

    Direct unit test against the consumer's publish hook, bypassing the real
    JetStream subscription entirely (see test_spine.py's requires_nats suite,
    and test_drain_once_publishes_state_despite_a_dead_spine below, for the
    full drain_once() path).
    """

    async def scenario() -> list[ActuatorState]:
        pwm_off = PwmLevel(duty=0.0)
        supervisor = InterlockSupervisor(on_event=_swallow)
        driver = SnappingFakeActuator("light-a", pwm_off)
        pwm_reg = registration("light-a", safe_state=pwm_off, actuator_class="pwm")
        supervisor.register(pwm_reg, driver)
        await supervisor.start()

        spine = FakeSpinePublisher()
        consumer = CommandConsumer(spine, supervisor)  # type: ignore[arg-type]

        cmd = command("light-a", PwmLevel(duty=0.05))
        outcome = await supervisor.apply(cmd)
        assert outcome == "applied"
        await consumer._publish_applied_state(cmd)

        await supervisor.stop()
        return spine.states

    states = run(scenario)
    assert len(states) == 1
    state = states[0]
    assert state.actuator_id == "light-a"
    assert state.level == PwmLevel(duty=0.0), "published the commanded 5%, not the snapped truth"
    assert state.reason == "commanded"
    assert state.latched is False


def test_applied_command_falls_back_to_commanded_level_when_read_back_is_none() -> None:
    """PCA9685 cannot report its own output — read_back() is honestly None.

    There the commanded level is the only thing worth publishing at all.
    """

    async def scenario() -> list[ActuatorState]:
        supervisor = InterlockSupervisor(on_event=_swallow)
        actuator = NoReadBackActuator("ato-pump", OFF)
        supervisor.register(registration("ato-pump"), actuator)
        await supervisor.start()

        spine = FakeSpinePublisher()
        consumer = CommandConsumer(spine, supervisor)  # type: ignore[arg-type]

        cmd = command("ato-pump", ON)
        outcome = await supervisor.apply(cmd)
        assert outcome == "applied"
        await consumer._publish_applied_state(cmd)

        await supervisor.stop()
        return spine.states

    states = run(scenario)
    assert len(states) == 1
    assert states[0].level == ON


def test_applied_command_falls_back_to_commanded_level_when_read_back_raises() -> None:
    """A read_back() failure (bus gone) must not block the publish entirely —
    it falls back to the commanded level rather than losing the state."""

    async def scenario() -> list[ActuatorState]:
        supervisor = InterlockSupervisor(on_event=_swallow)
        actuator = RaisingReadBackActuator("ato-pump", OFF)
        supervisor.register(registration("ato-pump"), actuator)
        await supervisor.start()

        spine = FakeSpinePublisher()
        consumer = CommandConsumer(spine, supervisor)  # type: ignore[arg-type]

        cmd = command("ato-pump", ON)
        outcome = await supervisor.apply(cmd)
        assert outcome == "applied"
        await consumer._publish_applied_state(cmd)  # must not raise

        await supervisor.stop()
        return spine.states

    states = run(scenario)
    assert len(states) == 1
    assert states[0].level == ON


def test_applied_command_publish_failure_does_not_raise() -> None:
    """A dead spine must not turn a successful actuation into an exception."""

    async def scenario() -> None:
        supervisor = InterlockSupervisor(on_event=_swallow)
        actuator = FakeActuator("ato-pump", OFF)
        supervisor.register(registration("ato-pump"), actuator)
        await supervisor.start()

        spine = FakeSpinePublisher(raises=True)
        consumer = CommandConsumer(spine, supervisor)  # type: ignore[arg-type]

        cmd = command("ato-pump", ON)
        outcome = await supervisor.apply(cmd)
        assert outcome == "applied"

        # Must not raise, even though the fake spine always does.
        await consumer._publish_applied_state(cmd)

        await supervisor.stop()

    run(scenario)


# ------------------------------------------------------- through drain_once


class FakeMeta:
    def __init__(self, num_delivered: int = 1) -> None:
        self.num_delivered = num_delivered


class FakeJsMsg:
    """Stands in for ``nats.aio.msg.Msg`` — only what drain_once() touches."""

    def __init__(self, data: bytes, *, subject: str = "bellasreef.cmd.binary.ato-pump") -> None:
        self.data = data
        self.subject = subject
        self.metadata = FakeMeta()
        self.acked = False
        self.termed = False

    async def ack(self) -> None:
        self.acked = True

    async def term(self) -> None:
        self.termed = True


class FakePullSubscription:
    """One batch of messages, then TimeoutError — matches a real fetch()
    against an empty stream closely enough for drain_once()'s purposes."""

    def __init__(self, msgs: list[FakeJsMsg]) -> None:
        self._msgs = msgs
        self._served = False

    async def fetch(self, batch: int, timeout: float) -> list[FakeJsMsg]:
        if self._served:
            raise TimeoutError
        self._served = True
        return self._msgs[:batch]


def test_drain_once_publishes_state_despite_a_dead_spine() -> None:
    """I4: pins the apply -> ack -> publish ordering that a direct
    ``_publish_applied_state`` call cannot — through drain_once() itself, with
    a fake JetStream pull subscription standing in for the real broker.

    A publish failure must not prevent the ack: the command already executed
    correctly (the whole point of ack-after-apply, not before), and failing
    to tell the wire about it must never turn into failing to acknowledge a
    command that actually ran — that would redeliver a command whose effect
    already happened.
    """

    async def scenario() -> tuple[list[str], bool, bool, int]:
        supervisor = InterlockSupervisor(on_event=_swallow)
        actuator = FakeActuator("ato-pump", OFF)
        supervisor.register(registration("ato-pump"), actuator)
        await supervisor.start()

        spine = FakeSpinePublisher(raises=True)
        consumer = CommandConsumer(spine, supervisor)  # type: ignore[arg-type]

        cmd = command("ato-pump", ON)
        msg = FakeJsMsg(cmd.model_dump_json().encode())
        consumer._sub = FakePullSubscription([msg])  # type: ignore[assignment]

        outcomes = await consumer.drain_once(timeout=1.0)

        await supervisor.stop()
        return [str(o) for o in outcomes], msg.acked, msg.termed, spine.attempts

    outcomes, acked, termed, attempts = run(scenario)
    assert outcomes == ["applied"]
    assert acked is True, "a dead spine must not block the ack for a command that ran"
    assert termed is False
    assert attempts == 1, "publish was never attempted"


# ------------------------------------------------------------------ startup


def test_startup_publishes_one_state_per_registered_actuator() -> None:
    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        actuator_a = FakeActuator("light-a", OFF)
        actuator_b = FakeActuator("light-b", OFF)
        service.supervisor.register(registration("light-a"), actuator_a)
        service.supervisor.register(registration("light-b"), actuator_b)
        await service.supervisor.start()
        service._registrations = [
            service.supervisor.registration_of("light-a"),
            service.supervisor.registration_of("light-b"),
        ]

        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        await service._publish_startup_states()
        # Snapshot before stop(): shutdown is itself one of item 3's autonomous
        # trips (see test_heartbeat_loss_publishes_safe_state's docstring), so
        # stopping the supervisor here would add its own "safe_state" entries
        # on top of the ones this test is checking.
        published = list(spine.states)
        await service.supervisor.stop()
        return published

    states = run(scenario)
    assert {s.actuator_id for s in states} == {"light-a", "light-b"}
    assert all(s.reason == "startup" for s in states)
    assert all(s.level == OFF for s in states)
    assert all(s.latched is False for s in states)


def test_startup_publish_with_no_spine_does_not_publish_or_crash() -> None:
    """Spine-less operation — the restart drill's own shape — must stay quiet."""

    async def scenario() -> None:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("drill-dummy", OFF)
        service.supervisor.register(registration("drill-dummy"), actuator)
        await service.supervisor.start()
        service._registrations = [service.supervisor.registration_of("drill-dummy")]

        assert service.spine is None
        await service._publish_startup_states()  # must not raise

        await service.supervisor.stop()

    run(scenario)


def test_startup_publish_failure_does_not_raise() -> None:
    async def scenario() -> None:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("light-a", OFF)
        service.supervisor.register(registration("light-a"), actuator)
        await service.supervisor.start()
        service._registrations = [service.supervisor.registration_of("light-a")]

        service.spine = FakeSpinePublisher(raises=True)  # type: ignore[assignment]
        await service._publish_startup_states()  # must not raise

        await service.supervisor.stop()

    run(scenario)


# ------------------------------------------------------- autonomous trips


def test_heartbeat_loss_publishes_safe_state() -> None:
    """Item 3: a trip the supervisor performs on its own must also reach the wire."""

    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("ato-pump", OFF)
        service.supervisor = InterlockSupervisor(on_event=service._on_safety_event)
        service.supervisor.register(registration("ato-pump"), actuator)
        await service.supervisor.start()
        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        # Command it on, then simulate the heartbeat watcher tripping directly
        # — the timing drills already cover the deadline behaviour itself
        # (test_drills.py); this test is only about what the trip publishes.
        await service.supervisor.apply(command("ato-pump", ON))
        await service._on_safety_event(
            SafetyEvent(
                actuator_id="ato-pump",
                reason="heartbeat_timeout",
                at=datetime.now(UTC),
                detail="no heartbeat for 30.0s",
            )
        )

        # Snapshot before stop(): shutdown is itself one of item 3's
        # autonomous trips, so stopping the supervisor would add its own
        # "safe_state" entry on top of the heartbeat one this test checks.
        published = list(spine.states)
        await service.supervisor.stop()
        return published

    states = run(scenario)
    assert len(states) == 1
    assert states[0].reason == "safe_state"
    assert states[0].level == OFF
    assert states[0].latched is False


def test_max_runtime_trip_publishes_latched_state() -> None:
    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("ato-pump", OFF)
        service.supervisor = InterlockSupervisor(on_event=service._on_safety_event)
        service.supervisor.register(registration("ato-pump"), actuator)
        await service.supervisor.start()
        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        # Latch it directly, as the runtime-deadline task would.
        service.supervisor._guards["ato-pump"].latched = True
        await service._on_safety_event(
            SafetyEvent(
                actuator_id="ato-pump",
                reason="max_runtime_exceeded",
                at=datetime.now(UTC),
                detail="ran continuously past max_runtime_s=3600.0",
            )
        )

        published = list(spine.states)
        await service.supervisor.stop()
        return published

    states = run(scenario)
    assert len(states) == 1
    assert states[0].reason == "interlock_latch"
    assert states[0].latched is True


def test_command_refusal_reasons_do_not_publish_state() -> None:
    """A refusal is not a transition — nothing about the actuator's level
    changed, so publishing here would be a lie about what happened."""

    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        for reason in ("clock_untrusted", "command_expired", "latched", "unknown_actuator"):
            await service._on_safety_event(
                SafetyEvent(
                    actuator_id="ato-pump",
                    reason=reason,
                    at=datetime.now(UTC),
                    detail="refused",
                )
            )

        return spine.states

    assert run(scenario) == []


# ------------------------------------------------------ I1 / I2: hook safety


def test_shutdown_event_that_did_not_reach_safe_state_is_not_published() -> None:
    """I2: a drive_safe() failure on the shutdown path (safety.py's start()
    and stop() except branches, ``reached_safe=False``) must not be reported
    as a safe transition — the real level is unknown, and publishing anything
    would be a guess."""

    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("ato-pump", OFF)
        service.supervisor.register(registration("ato-pump"), actuator)
        await service.supervisor.start()
        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        await service._on_safety_event(
            SafetyEvent(
                actuator_id="ato-pump",
                reason="shutdown",
                at=datetime.now(UTC),
                detail="drive_safe FAILED during shutdown: RuntimeError('welded relay')",
                reached_safe=False,
            )
        )

        published = list(spine.states)
        await service.supervisor.stop()
        return published

    assert run(scenario) == []


def test_shutdown_event_that_reached_safe_state_is_published() -> None:
    """The contrast case: a normal, successful shutdown trip still publishes."""

    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("ato-pump", OFF)
        service.supervisor.register(registration("ato-pump"), actuator)
        await service.supervisor.start()
        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        await service._on_safety_event(
            SafetyEvent(
                actuator_id="ato-pump",
                reason="shutdown",
                at=datetime.now(UTC),
                detail="supervisor stopping",
            )
        )

        published = list(spine.states)
        await service.supervisor.stop()
        return published

    states = run(scenario)
    assert len(states) == 1
    assert states[0].reason == "safe_state"


def test_on_safety_event_for_an_unregistered_actuator_does_not_raise() -> None:
    """I1: this callback runs *inside* safety.py's own call stack — a lookup
    failure or any other bookkeeping bug here must never propagate back into
    an interlock trip. An actuator this HardwareIO has never registered
    (registration_of() would KeyError) is the easiest way to force that path."""

    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        # No actuator registered at all: registration_of("ghost") raises.
        await service._on_safety_event(
            SafetyEvent(
                actuator_id="ghost",
                reason="heartbeat_timeout",
                at=datetime.now(UTC),
                detail="no heartbeat for 30.0s",
            )
        )  # must not raise

        return spine.states

    assert run(scenario) == []

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
from bellasreef_hardware_io import (
    FakeActuator,
    InterlockSupervisor,
    SafetyEvent,
    SnappingFakeActuator,
)
from bellasreef_hardware_io.app import HardwareIO
from bellasreef_hardware_io.spine import CommandConsumer

OFF = BinaryLevel(on=False)
ON = BinaryLevel(on=True)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class FakeSpinePublisher:
    """Records every ActuatorState handed to publish_state.

    Duck-typed against Spine's async publish surface — only the methods the
    call sites actually use. ``raises`` lets a test prove a broken spine
    cannot break actuation or startup; ``attempts`` counts every call even
    when it raises, so a test can prove a publish was *tried* without
    depending on it having recorded anything. ``audit_raises`` is the same
    idea for ``publish_audit`` — independent of ``raises``, because a broken
    audit publish must not be conflated with a broken state publish.
    """

    def __init__(self, *, raises: bool = False, audit_raises: bool = False) -> None:
        self.states: list[ActuatorState] = []
        self.raises = raises
        self.attempts = 0
        self.closed = False
        self.audit_raises = audit_raises
        self.audits: list[tuple[str, dict[str, object]]] = []

    async def publish_state(self, state: ActuatorState) -> None:
        self.attempts += 1
        if self.raises:
            raise RuntimeError("spine unreachable")
        if self.closed:
            # Mirrors the real Spine: close() drops the JetStream context and
            # any publish after it raises from the ``js`` property. A test
            # that lets a closed spine record states would prove nothing
            # about shutdown ordering.
            raise RuntimeError("spine not connected")
        self.states.append(state)

    async def publish_audit(self, category: str, event: dict[str, object]) -> None:
        if self.audit_raises:
            raise RuntimeError("audit stream unreachable")
        self.audits.append((category, event))

    async def close(self) -> None:
        self.closed = True


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


def expired_command(actuator_id: str, level: ActuatorLevel = ON) -> ActuatorCommand:
    """Already past its own TTL. ``expires_at`` must be strictly after
    ``emitted_at`` (contract validation), so both sit in the past relative
    to ``now`` rather than ``emitted_at`` sitting at ``now``."""
    now = datetime.now(UTC)
    return ActuatorCommand(
        message_id=uuid4(),
        emitted_at=now - timedelta(seconds=10),
        source="control-engine",
        actuator_id=actuator_id,
        actuator_class=level.kind,
        level=level,
        idempotency_key=uuid4(),
        expires_at=now - timedelta(seconds=1),
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
        self.naked_with_delay: float | None = None

    async def ack(self) -> None:
        self.acked = True

    async def term(self) -> None:
        self.termed = True

    async def nak(self, delay: float | None = None) -> None:
        # nats-py's real Msg.nak(delay=...) takes seconds and converts to ns
        # internally; the fake records the seconds value a test asserts on.
        self.naked_with_delay = delay


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


def test_driver_failure_naks_instead_of_killing() -> None:
    """Finding 5: the PCA9685 physically dropped off bus 1 on 2026-08-15. A
    driver OSError mid-apply must nak the message, not unwind the process —
    the un-acked workqueue message was redelivering into every restart."""

    async def scenario() -> tuple[list[str], float | None, bool, bool]:
        supervisor = InterlockSupervisor(on_event=_swallow)
        actuator = FakeActuator("pca9685-0", OFF)
        actuator.apply_raises = OSError("Remote I/O error")
        supervisor.register(registration("pca9685-0"), actuator)
        await supervisor.start()

        spine = FakeSpinePublisher()
        consumer = CommandConsumer(spine, supervisor)  # type: ignore[arg-type]

        cmd = command("pca9685-0", ON)
        msg = FakeJsMsg(cmd.model_dump_json().encode())
        consumer._sub = FakePullSubscription([msg])  # type: ignore[assignment]

        outcomes = await consumer.drain_once(timeout=1.0)

        await supervisor.stop()
        return [str(o) for o in outcomes], msg.naked_with_delay, msg.acked, msg.termed

    outcomes, naked_with_delay, acked, termed = run(scenario)
    assert outcomes == [], "nothing applied, nothing raised"
    assert naked_with_delay == 1.0
    assert not acked
    assert not termed


def test_audit_publish_failure_does_not_lose_the_term() -> None:
    """A broker blip during a refusal's audit publish must not convert a
    routine refusal into a service restart. The term still lands."""

    async def scenario() -> tuple[list[str], bool]:
        supervisor = InterlockSupervisor(on_event=_swallow)
        actuator = FakeActuator("pca9685-0", OFF)
        supervisor.register(registration("pca9685-0"), actuator)
        await supervisor.start()

        spine = FakeSpinePublisher(audit_raises=True)
        consumer = CommandConsumer(spine, supervisor)  # type: ignore[arg-type]

        cmd = expired_command("pca9685-0")
        msg = FakeJsMsg(cmd.model_dump_json().encode())
        consumer._sub = FakePullSubscription([msg])  # type: ignore[assignment]

        outcomes = await consumer.drain_once(timeout=1.0)

        await supervisor.stop()
        return [str(o) for o in outcomes], msg.termed

    outcomes, termed = run(scenario)
    assert outcomes == ["rejected_expired"]
    assert termed


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


# ------------------------------------------------------- reconnect republish


def test_republish_safe_states_publishes_only_for_actuators_at_safe_state() -> None:
    """2026-08-23 NATS-outage drill: a spine outage longer than the heartbeat
    timeout trips actuators dark, but the trip-state publish fails into the
    down spine — swallowed by design, observed 22:49:23Z ("failed to publish
    actuator state ... reason=safe_state" for both actuators). The engine's
    duty memory is never corrected, so its first post-recovery command
    re-energizes the dark channel in ONE step at curve duty (observed:
    0 -> 11.5% in a single lighting:ramp command at 22:50:57Z) instead of
    slewing up from dark.

    Wired as ``Spine.on_reconnected``, ``_republish_safe_states`` republishes
    the declared safe_state for exactly the actuators the supervisor reports
    at safe state right now — light-a here, freshly started and untouched.
    light-b is commanded on and stays held there: its level is still true on
    the hardware, so republishing it would be noise, and — per the brief —
    would risk a spurious dark-dip on a sub-timeout blip that never actually
    tripped anything.
    """

    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        # A fresh supervisor with the default clock_trusted=True: this test
        # applies a command, and HardwareIO's own supervisor takes its clock
        # trust from the real host clock (clock_is_trusted()) at __init__ —
        # the same substitution test_heartbeat_loss_publishes_safe_state uses,
        # for the same reason.
        service.supervisor = InterlockSupervisor(on_event=service._on_safety_event)
        dark = FakeActuator("light-a", OFF)
        held = FakeActuator("light-b", OFF)
        service.supervisor.register(registration("light-a"), dark)
        service.supervisor.register(registration("light-b"), held)
        await service.supervisor.start()
        service._registrations = [
            service.supervisor.registration_of("light-a"),
            service.supervisor.registration_of("light-b"),
        ]

        await service.supervisor.apply(command("light-b", ON))
        assert not service.supervisor.is_at_safe_state("light-b")
        assert service.supervisor.is_at_safe_state("light-a")

        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        await service._republish_safe_states()

        published = list(spine.states)
        await service.supervisor.stop()
        return published

    states = run(scenario)
    assert len(states) == 1
    assert states[0].actuator_id == "light-a"
    assert states[0].level == OFF
    assert states[0].reason == "safe_state"
    assert states[0].latched is False


def test_republish_safe_states_includes_a_channel_left_dark_by_a_snap_band_command() -> None:
    """A snap-band command must not make the reconnect republish skip a
    channel that is actually dark.

    2026-08-29 finding: InterlockSupervisor's runtime-cap bookkeeping used to
    compare the COMMANDED level against the declared safe state, so a 5% PWM
    command — genuinely dark at the pin, per dimming.py's snap_duty rule —
    read as "not at safe state". ``is_at_safe_state()`` feeds straight into
    ``_republish_safe_states`` (see the test above), so a channel a
    dawn/dusk ramp's low end drove dark would be silently skipped on
    reconnect, leaving the engine's duty memory uncorrected — the exact bug
    the 2026-08-23 republish fix exists to prevent, reached from the snap-band
    side instead of the heartbeat/runtime-trip side.
    """

    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        service.supervisor = InterlockSupervisor(on_event=service._on_safety_event)
        pwm_off = PwmLevel(duty=0.0)
        driver = SnappingFakeActuator("light-a", pwm_off)
        service.supervisor.register(
            registration("light-a", safe_state=pwm_off, actuator_class="pwm"), driver
        )
        await service.supervisor.start()
        service._registrations = [service.supervisor.registration_of("light-a")]

        await service.supervisor.apply(command("light-a", PwmLevel(duty=0.05)))
        assert service.supervisor.is_at_safe_state("light-a"), (
            "the pin is genuinely dark (snapped); the guard must agree"
        )

        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        await service._republish_safe_states()

        published = list(spine.states)
        await service.supervisor.stop()
        return published

    states = run(scenario)
    assert len(states) == 1, "a snap-band command must not make republish skip the channel"
    assert states[0].actuator_id == "light-a"
    assert states[0].level == PwmLevel(duty=0.0)
    assert states[0].reason == "safe_state"


def test_republish_safe_states_carries_the_current_latch_flag() -> None:
    async def scenario() -> list[ActuatorState]:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("ato-pump", OFF)
        service.supervisor.register(registration("ato-pump"), actuator)
        await service.supervisor.start()
        service._registrations = [service.supervisor.registration_of("ato-pump")]

        # At safe state, but latched by a separate mechanism (mirrors
        # test_max_runtime_trip_publishes_latched_state's direct latch).
        service.supervisor._guards["ato-pump"].latched = True

        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        await service._republish_safe_states()

        published = list(spine.states)
        await service.supervisor.stop()
        return published

    states = run(scenario)
    assert len(states) == 1
    assert states[0].latched is True


def test_republish_safe_states_with_no_spine_does_not_publish_or_crash() -> None:
    async def scenario() -> None:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("ato-pump", OFF)
        service.supervisor.register(registration("ato-pump"), actuator)
        await service.supervisor.start()
        service._registrations = [service.supervisor.registration_of("ato-pump")]

        assert service.spine is None
        await service._republish_safe_states()  # must not raise

        await service.supervisor.stop()

    run(scenario)


def test_republish_safe_states_publish_failure_does_not_raise() -> None:
    async def scenario() -> None:
        service = HardwareIO(metrics_port=0)
        actuator = FakeActuator("ato-pump", OFF)
        service.supervisor.register(registration("ato-pump"), actuator)
        await service.supervisor.start()
        service._registrations = [service.supervisor.registration_of("ato-pump")]

        service.spine = FakeSpinePublisher(raises=True)  # type: ignore[assignment]
        await service._republish_safe_states()  # must not raise

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


def test_shutdown_publishes_each_actuator_safe_state_before_closing_the_spine() -> None:
    """The real ``shutdown()`` path, not a hand-injected event.

    Item 3 put "shutdown" among the autonomous trips, then app.py's own
    comment conceded it never reached the wire: ``shutdown()`` closed the
    spine *before* ``supervisor.stop()`` drove the actuators safe, so every
    shutdown state was published into a closed connection — one WARNING with
    a traceback per actuator on every clean stop (seen on the 2026-08-17
    adoption restart), and a real transition (a light going dark) that the
    API never learned about until the next process's startup publish. The
    supervisor must stop while the spine is live; the spine closes last.
    """

    async def scenario() -> tuple[list[ActuatorState], bool]:
        service = HardwareIO(metrics_port=0)
        service.supervisor.register(registration("light-a"), FakeActuator("light-a", OFF))
        service.supervisor.register(registration("light-b"), FakeActuator("light-b", OFF))
        await service.supervisor.start()
        spine = FakeSpinePublisher()
        service.spine = spine  # type: ignore[assignment]

        await service.shutdown()
        return list(spine.states), spine.closed

    states, closed = run(scenario)
    assert closed is True
    shutdown_states = [s for s in states if s.reason == "safe_state"]
    assert {s.actuator_id for s in shutdown_states} == {"light-a", "light-b"}
    assert all(s.level == OFF for s in shutdown_states)


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

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""ActuatorState publishing: on every applied command, and once per actuator
at startup.

Before this, Spine.publish_state() had zero production callers — the hub never
told the wire what any actuator's level was, so every client showed "no state
yet" forever. These tests exercise the two production call sites directly,
against a fake in-memory spine, so they run with no NATS at all.

Both call sites must survive a spine that raises (or is simply absent):
publish failures are a logged-and-continue concern, never a reason to fail a
command or crash startup — matching how ``_publish_reading`` already treats a
failed sensor publish.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from bellasreef_contracts import (
    ActuatorCommand,
    ActuatorRegistration,
    ActuatorState,
    BinaryLevel,
)
from bellasreef_hardware_io import FakeActuator, InterlockSupervisor, SafetyEvent
from bellasreef_hardware_io.app import HardwareIO
from bellasreef_hardware_io.spine import CommandConsumer

OFF = BinaryLevel(on=False)
ON = BinaryLevel(on=True)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class FakeSpinePublisher:
    """Records every ActuatorState handed to publish_state.

    Duck-typed against Spine's async publish surface — only the one method
    either call site actually uses. ``raises`` lets a test prove a broken
    spine cannot break actuation or startup.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.states: list[ActuatorState] = []
        self.raises = raises

    async def publish_state(self, state: ActuatorState) -> None:
        if self.raises:
            raise RuntimeError("spine unreachable")
        self.states.append(state)


def registration(
    actuator_id: str,
    *,
    safe_state: BinaryLevel = OFF,
) -> ActuatorRegistration:
    return ActuatorRegistration(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=actuator_id,
        actuator_class="binary",
        role="outlet",
        driver_id="fake-actuator",
        control_authority="authoritative",
        failsafe_capable=True,
        transport="local",
        safe_state=safe_state,
        max_runtime_s=3600.0,
        heartbeat_timeout_s=30.0,
    )


def command(actuator_id: str, level: BinaryLevel = ON) -> ActuatorCommand:
    now = datetime.now(UTC)
    return ActuatorCommand(
        message_id=uuid4(),
        emitted_at=now,
        source="control-engine",
        actuator_id=actuator_id,
        actuator_class="binary",
        level=level,
        idempotency_key=uuid4(),
        expires_at=now + timedelta(seconds=60),
    )


# --------------------------------------------------------- applied commands


def test_applied_command_publishes_state_with_commanded_level() -> None:
    """The headline behaviour: apply() succeeding must tell the wire.

    Direct unit test against the consumer's publish hook, bypassing the real
    JetStream subscription entirely — drain_once() needs a live pull
    subscription (see test_spine.py's requires_nats suite for that), but the
    publish-on-apply behaviour does not depend on how the command arrived.
    """

    async def scenario() -> list[ActuatorState]:
        supervisor = InterlockSupervisor(on_event=_swallow)
        actuator = FakeActuator("ato-pump", OFF)
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
    state = states[0]
    assert state.actuator_id == "ato-pump"
    assert state.level == ON
    assert state.reason == "commanded"
    assert state.latched is False


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


async def _swallow(event: SafetyEvent) -> None:
    return None


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

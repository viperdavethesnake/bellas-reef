# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Fail-safe drills.

PRD G2: kill the hub process, kill the container runtime, drop the spine — every
actuator reaches its declared safe state within its timeout, every time.

These run against fakes and never touch hardware. The power-pull drill is not
here: no software can assert it, because it depends on relays being wired
normally-open. It is a bench procedure in docs/host-setup.md.

Timing note: the heartbeat drills assert *measured* elapsed time against the
declared `heartbeat_timeout_s`, with a tight upper bound. That upper bound is
the point — an implementation that polled for expiry would satisfy "eventually
goes safe" while missing the deadline by up to one poll interval, and would pass
a laxer test while being wrong on a real tank.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import (
    ActuatorCommand,
    ActuatorRegistration,
    BinaryLevel,
)
from bellasreef_hardware_io import FakeActuator, InterlockSupervisor, SafetyEvent

# Timeouts are short so the suite stays fast, and deliberately not round
# numbers so an implementation that happened to poll on a 50/100 ms cadence
# could not pass by coincidence.
HEARTBEAT_TIMEOUT_S = 0.17
MAX_RUNTIME_S = 0.23

#: Scheduler jitter allowance. Generous enough not to flake on a loaded CI
#: runner, tight enough that a one-second poll loop could never fit inside it.
JITTER_S = 0.09

OFF = BinaryLevel(on=False)
ON = BinaryLevel(on=True)


def _registration(
    actuator_id: str = "ato-pump",
    *,
    heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
    max_runtime_s: float = MAX_RUNTIME_S,
) -> ActuatorRegistration:
    return ActuatorRegistration(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=actuator_id,
        actuator_class="binary",
        role="outlet",
        driver_id="fake-actuator",
        safe_state=OFF,
        max_runtime_s=max_runtime_s,
        heartbeat_timeout_s=heartbeat_timeout_s,
    )


def _command(
    actuator_id: str = "ato-pump", *, on: bool = True, ttl_s: float = 30.0
) -> ActuatorCommand:
    now = datetime.now(UTC)
    return ActuatorCommand(
        message_id=uuid4(),
        emitted_at=now,
        source="control-engine",
        actuator_id=actuator_id,
        actuator_class="binary",
        level=BinaryLevel(on=on),
        idempotency_key=uuid4(),
        expires_at=now + timedelta(seconds=ttl_s),
    )


class Recorder:
    """Collects safety events and lets a test await the next one."""

    def __init__(self) -> None:
        self.events: list[SafetyEvent] = []
        self.stamps: list[float] = []
        self._arrived = asyncio.Event()

    async def __call__(self, event: SafetyEvent) -> None:
        self.stamps.append(asyncio.get_running_loop().time())
        self.events.append(event)
        self._arrived.set()

    async def wait_for(self, reason: str, timeout: float) -> SafetyEvent:
        async with asyncio.timeout(timeout):
            while True:
                for event in self.events:
                    if event.reason == reason:
                        return event
                self._arrived.clear()
                await self._arrived.wait()

    def stamp_of(self, reason: str) -> float:
        for event, stamp in zip(self.events, self.stamps, strict=True):
            if event.reason == reason:
                return stamp
        raise AssertionError(f"no {reason} event recorded")


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


# ------------------------------------------------------------ drill: heartbeat


def test_drill_heartbeat_loss_goes_safe_at_the_declared_deadline() -> None:
    """Safe state must arrive AT the timeout, not one poll cycle later."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(), actuator)

        await sup.start()
        sup.heartbeat()
        assert await sup.apply(_command()) == "applied"
        assert not actuator.is_safe(), "precondition: pump is running"

        # Last beat. Nothing beats again — the controller is gone.
        sup.heartbeat()
        t0 = asyncio.get_running_loop().time()

        await rec.wait_for("heartbeat_timeout", timeout=2.0)
        elapsed = rec.stamp_of("heartbeat_timeout") - t0

        assert actuator.is_safe(), "pump must be off"

        # Not early: firing before the declared timeout would mean a healthy
        # controller could be cut off mid-operation.
        assert elapsed >= HEARTBEAT_TIMEOUT_S * 0.95, (
            f"tripped early: {elapsed:.4f}s < {HEARTBEAT_TIMEOUT_S}s"
        )
        # Not late: this is the assertion that a poll loop fails.
        assert elapsed < HEARTBEAT_TIMEOUT_S + JITTER_S, (
            f"tripped late: {elapsed:.4f}s vs declared {HEARTBEAT_TIMEOUT_S}s "
            f"(+{JITTER_S}s allowance) — is expiry being polled rather than scheduled?"
        )

        await sup.stop()

    run(scenario)


def test_drill_heartbeat_timeout_scales_with_declared_value() -> None:
    """A longer declared timeout must actually wait longer.

    Guards against a fixed internal interval that happens to satisfy one case.
    """

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("slow-guy", OFF)
        long_timeout = HEARTBEAT_TIMEOUT_S * 3
        sup.register(_registration("slow-guy", heartbeat_timeout_s=long_timeout), actuator)

        await sup.start()
        sup.heartbeat()
        await sup.apply(_command("slow-guy"))
        sup.heartbeat()
        t0 = asyncio.get_running_loop().time()

        await rec.wait_for("heartbeat_timeout", timeout=3.0)
        elapsed = rec.stamp_of("heartbeat_timeout") - t0

        assert elapsed >= long_timeout * 0.95
        assert elapsed < long_timeout + JITTER_S
        await sup.stop()

    run(scenario)


def test_drill_heartbeats_keep_the_actuator_running() -> None:
    """The watchdog must not trip while the controller is alive."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(max_runtime_s=10.0), actuator)

        await sup.start()
        sup.heartbeat()
        await sup.apply(_command())

        # Beat well inside the timeout, for several times the timeout window.
        deadline = asyncio.get_running_loop().time() + HEARTBEAT_TIMEOUT_S * 4
        while asyncio.get_running_loop().time() < deadline:
            sup.heartbeat()
            await asyncio.sleep(HEARTBEAT_TIMEOUT_S / 4)

        assert not actuator.is_safe(), "healthy heartbeats must not trip the watchdog"
        assert [e for e in rec.events if e.reason == "heartbeat_timeout"] == []
        await sup.stop()

    run(scenario)


def test_drill_actuator_does_not_spring_back_on_when_heartbeats_return() -> None:
    """Recovery is not automatic. Safe state persists until commanded."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(), actuator)

        await sup.start()
        sup.heartbeat()
        await sup.apply(_command())
        sup.heartbeat()

        await rec.wait_for("heartbeat_timeout", timeout=2.0)
        assert actuator.is_safe()

        # Controller comes back.
        for _ in range(5):
            sup.heartbeat()
            await asyncio.sleep(HEARTBEAT_TIMEOUT_S / 4)

        assert actuator.is_safe(), "must stay safe until an explicit command arrives"
        await sup.stop()

    run(scenario)


# ------------------------------------------------------- drill: process death


def test_drill_process_shutdown_drives_every_actuator_safe() -> None:
    """Graceful stop — the SIGTERM path."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        pumps = [FakeActuator(f"pump-{i}", OFF) for i in range(3)]
        for i, pump in enumerate(pumps):
            sup.register(_registration(f"pump-{i}", max_runtime_s=10.0), pump)

        await sup.start()
        sup.heartbeat()
        for i in range(3):
            await sup.apply(_command(f"pump-{i}"))
        assert all(not p.is_safe() for p in pumps)

        await sup.stop()

        assert all(p.is_safe() for p in pumps), "every actuator safe on shutdown"
        assert {e.actuator_id for e in rec.events if e.reason == "shutdown"} == {
            "pump-0",
            "pump-1",
            "pump-2",
        }

    run(scenario)


def test_drill_one_broken_driver_does_not_block_the_others() -> None:
    """A driver that throws on drive_safe must not strand its neighbours.

    This is the failure mode where a single bad device turns a safe shutdown
    into a tank full of running equipment.
    """

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        good_a = FakeActuator("good-a", OFF)
        broken = FakeActuator("broken", OFF)
        good_b = FakeActuator("good-b", OFF)

        for name, drv in (("good-a", good_a), ("broken", broken), ("good-b", good_b)):
            sup.register(_registration(name, max_runtime_s=10.0), drv)

        await sup.start()
        sup.heartbeat()
        for name in ("good-a", "good-b"):
            await sup.apply(_command(name))

        # The driver breaks while running — the realistic case. `broken` is
        # registered between the two good ones, so a bare loop would strand
        # good-b.
        broken.fail_safe_raises = True

        with pytest.raises(ExceptionGroup):
            await sup.stop()

        assert good_a.is_safe(), "actuator before the broken one must be safe"
        assert good_b.is_safe(), "actuator AFTER the broken one must also be safe"
        assert broken.safe_calls > 0, "the broken driver was still asked"

    run(scenario)


def test_drill_startup_latches_an_actuator_it_cannot_prove_safe() -> None:
    """If we cannot drive it safe at boot, we will not command it later."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        good = FakeActuator("good", OFF)
        bad = FakeActuator("bad", OFF)
        bad.fail_safe_raises = True
        sup.register(_registration("good", max_runtime_s=10.0), good)
        sup.register(_registration("bad", max_runtime_s=10.0), bad)

        with pytest.raises(ExceptionGroup):
            await sup.start()

        assert sup.is_latched("bad"), "unprovable actuator must be latched"
        assert not sup.is_latched("good"), "its neighbour must be unaffected"

        sup.heartbeat()
        assert await sup.apply(_command("bad")) == "rejected_latched"
        assert await sup.apply(_command("good")) == "applied"

        bad.fail_safe_raises = False
        await sup.stop()

    run(scenario)


# ------------------------------------------------------------ drill: max runtime


def test_drill_max_runtime_trips_and_latches() -> None:
    """R2: the runtime cap is enforced below the control logic and latches."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(max_runtime_s=MAX_RUNTIME_S), actuator)

        await sup.start()
        sup.heartbeat()

        t0 = asyncio.get_running_loop().time()
        await sup.apply(_command())

        # Keep heartbeats healthy so this is unambiguously the runtime cap.
        async def beat() -> None:
            while True:
                sup.heartbeat()
                await asyncio.sleep(HEARTBEAT_TIMEOUT_S / 4)

        beater = asyncio.create_task(beat())
        await rec.wait_for("max_runtime_exceeded", timeout=3.0)
        beater.cancel()

        elapsed = rec.stamp_of("max_runtime_exceeded") - t0
        assert elapsed >= MAX_RUNTIME_S * 0.95
        assert elapsed < MAX_RUNTIME_S + JITTER_S

        assert actuator.is_safe()
        assert sup.is_latched("ato-pump")

        # Latched means latched: further commands are refused.
        assert await sup.apply(_command()) == "rejected_latched"
        assert actuator.is_safe()

        await sup.stop()

    run(scenario)


def test_drill_latch_clears_only_by_operator_action() -> None:
    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(max_runtime_s=0.05), actuator)

        await sup.start()
        sup.heartbeat()
        await sup.apply(_command())
        await rec.wait_for("max_runtime_exceeded", timeout=2.0)
        assert sup.is_latched("ato-pump")

        await sup.clear_latch("ato-pump", operator="david")
        assert not sup.is_latched("ato-pump")

        sup.heartbeat()
        assert await sup.apply(_command()) == "applied"
        await sup.stop()

    run(scenario)


def test_drill_returning_to_safe_state_resets_the_runtime_cap() -> None:
    """The cap is on *continuous* runtime, so a real duty cycle is not punished."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("heater", OFF)
        sup.register(_registration("heater", max_runtime_s=MAX_RUNTIME_S), actuator)

        await sup.start()
        sup.heartbeat()

        # Three bursts, each well under the cap, totalling more than the cap.
        for _ in range(3):
            await sup.apply(_command("heater", on=True))
            await asyncio.sleep(MAX_RUNTIME_S * 0.5)
            await sup.apply(_command("heater", on=False))
            await asyncio.sleep(0.01)

        assert not sup.is_latched("heater")
        assert [e for e in rec.events if e.reason == "max_runtime_exceeded"] == []
        await sup.stop()

    run(scenario)


# --------------------------------------------------------- drill: spine outage


def test_drill_nats_outage_is_indistinguishable_from_a_dead_controller() -> None:
    """The spine going away must produce the same safe state as a dead engine.

    There is nothing to mock: the supervisor never touches NATS. That is the
    design — an interlock that needed the broker would fail exactly when the
    broker did. This test documents the property by exercising the supervisor
    with no spine present at all.
    """

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("return-pump", OFF)
        sup.register(_registration("return-pump"), actuator)

        await sup.start()
        sup.heartbeat()
        await sup.apply(_command("return-pump"))
        sup.heartbeat()

        event = await rec.wait_for("heartbeat_timeout", timeout=2.0)
        assert actuator.is_safe()
        assert event.actuator_id == "return-pump"
        await sup.stop()

    run(scenario)


# ------------------------------------------------------------- command gating


def test_expired_command_is_refused() -> None:
    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(), actuator)
        await sup.start()
        sup.heartbeat()

        stale = _command(ttl_s=0.01)
        await asyncio.sleep(0.05)

        assert await sup.apply(stale) == "rejected_expired"
        assert actuator.is_safe()
        assert any(e.reason == "command_expired" for e in rec.events)
        await sup.stop()

    run(scenario)


def test_commands_are_refused_when_the_clock_is_not_trusted() -> None:
    """No RTC battery: after a power cut the clock is wrong until chrony syncs.

    An expiry decision made against a wrong clock is not a decision.
    """

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec, clock_trusted=False)
        actuator = FakeActuator("doser", OFF)
        sup.register(_registration("doser"), actuator)
        await sup.start()
        sup.heartbeat()

        assert await sup.apply(_command("doser")) == "rejected_clock"
        assert actuator.is_safe()
        assert any(e.reason == "clock_untrusted" for e in rec.events)
        await sup.stop()

    run(scenario)


def test_actuators_start_at_safe_state() -> None:
    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        actuator.level = ON  # hardware came up in an unknown state
        sup.register(_registration(), actuator)

        await sup.start()
        assert actuator.is_safe(), "startup must assert safe state, not assume it"
        await sup.stop()

    run(scenario)

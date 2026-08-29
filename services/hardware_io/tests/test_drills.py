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
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import (
    ActuatorCommand,
    ActuatorLevel,
    ActuatorRegistration,
    BinaryLevel,
    PwmLevel,
)
from bellasreef_hardware_io import (
    FakeActuator,
    InterlockSupervisor,
    SafetyEvent,
    SnappingFakeActuator,
    safety,
)

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
        control_authority="authoritative",
        failsafe_capable=True,
        transport="local",
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


PWM_OFF = PwmLevel(duty=0.0)


def _pwm_registration(
    actuator_id: str = "light-a",
    *,
    heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
    max_runtime_s: float = MAX_RUNTIME_S,
) -> ActuatorRegistration:
    """Same shape as :func:`_registration`, for the PWM/snap-band drills —
    these need an ``actuator_class="pwm"`` guard with a ``PwmLevel`` safe
    state, which :func:`_registration`'s binary pumps/heaters don't exercise.
    """
    return ActuatorRegistration(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=actuator_id,
        actuator_class="pwm",
        role="light",
        driver_id="fake-pwm",
        control_authority="authoritative",
        failsafe_capable=True,
        transport="local",
        safe_state=PWM_OFF,
        max_runtime_s=max_runtime_s,
        heartbeat_timeout_s=heartbeat_timeout_s,
    )


def _pwm_command(
    actuator_id: str = "light-a", *, duty: float, ttl_s: float = 30.0
) -> ActuatorCommand:
    now = datetime.now(UTC)
    return ActuatorCommand(
        message_id=uuid4(),
        emitted_at=now,
        source="control-engine",
        actuator_id=actuator_id,
        actuator_class="pwm",
        level=PwmLevel(duty=duty),
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


async def _null_sink(event: SafetyEvent) -> None:
    """An on_event sink for tests that only care about actuator state."""


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float, interval: float = 0.01
) -> None:
    """Poll for a condition instead of guessing a fixed sleep.

    Used by the retry tests below, where the exact settle time depends on a
    patched backoff rather than a declared deadline — the drills above assert
    on the deadline itself, this asserts on the eventual outcome.
    """
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


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


def test_apply_while_heartbeat_lost_still_applies_but_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No-flap stays no-flap: a command mid-trip is still applied, not
    refused — that is the normal recovery path for a live controller's first
    post-trip command. But an engine rolled back to a pre-heartbeat build
    would command this actuator while never beating, and that mixed-version
    state must not be silent.
    """

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(), actuator)

        await sup.start()
        await rec.wait_for("heartbeat_timeout", timeout=2.0)
        assert actuator.is_safe()

        with caplog.at_level(logging.WARNING, logger="bellasreef_hardware_io.safety"):
            outcome = await sup.apply(_command(on=True))

        assert outcome == "applied"
        assert not actuator.is_safe()
        assert any("heartbeat is lost" in r.message for r in caplog.records)
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


# ------------------------------------------- drill: max runtime keys on the
# ------------------------------------------- effective (post-snap) level


def test_a_snap_band_command_starts_no_runtime_clock() -> None:
    """Extends test_drill_max_runtime_trips_and_latches's area: the runtime
    cap must key on what the hardware actually does, not what was asked for.

    2026-08-29 finding: dimming.py's snap_duty rule (session-4 ruling,
    CLAUDE.md) turns any duty under 8% into a genuinely dark pin, but
    InterlockSupervisor used to compare the COMMANDED level (0.049) against
    the declared safe state (0.0) — a mismatch that started the runtime-cap
    clock on a channel already dark at the pin. A dawn/dusk ramp crosses this
    band twice a day, so this was the ordinary path, not an edge case: after
    LIGHT_MAX_RUNTIME_S the guard would latch a dark channel, and clearing a
    latch is an explicit operator action.

    The behavioural half, mirroring
    :func:`test_drill_returning_to_safe_state_resets_the_runtime_cap`: it is
    not enough that ``runtime_task`` reads ``None`` immediately after the
    command — the test also has to actually sleep past ``MAX_RUNTIME_S`` and
    prove nothing latches later. ``runtime_task is None`` alone would still
    pass if some other code path started a clock a beat later.
    """

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = SnappingFakeActuator("light-a", PWM_OFF)
        sup.register(_pwm_registration(), actuator)

        await sup.start()
        sup.heartbeat()

        outcome = await sup.apply(_pwm_command(duty=0.049))
        assert outcome == "applied"

        assert sup.is_at_safe_state("light-a"), (
            "the pin is genuinely dark (snapped); the guard must agree"
        )
        assert sup._guards["light-a"].runtime_task is None, (
            "a snap-band command must not start the max-runtime clock on a dark pin"
        )

        # Keep heartbeats healthy so a latch here could only be the runtime
        # cap, then sleep well past it and prove it never fires.
        async def beat() -> None:
            while True:
                sup.heartbeat()
                await asyncio.sleep(HEARTBEAT_TIMEOUT_S / 4)

        beater = asyncio.create_task(beat())
        await asyncio.sleep(MAX_RUNTIME_S + JITTER_S)
        beater.cancel()

        assert not sup.is_latched("light-a"), (
            "a snap-band command must not latch the channel once the cap elapses"
        )
        assert [e for e in rec.events if e.reason == "max_runtime_exceeded"] == []

        await sup.stop()

    run(scenario)


def test_exactly_eight_percent_starts_the_runtime_clock() -> None:
    """The 8% floor (MIN_USABLE_DUTY, dimming.py) is honoured, not snapped —
    the pin is genuinely lit, so the runtime cap must guard it exactly as it
    would any other non-safe level."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = SnappingFakeActuator("light-a", PWM_OFF)
        sup.register(_pwm_registration(), actuator)

        await sup.start()
        sup.heartbeat()

        outcome = await sup.apply(_pwm_command(duty=0.08))
        assert outcome == "applied"

        assert not sup.is_at_safe_state("light-a")
        task = sup._guards["light-a"].runtime_task
        assert task is not None and not task.done(), (
            "8% is honoured, not snapped; the runtime clock must be running"
        )

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


# ------------------------------------------------------- drill: late registration


def test_register_after_start_gets_a_watcher() -> None:
    """A production actuator is registered AFTER start() (app.py builds from the
    registry only once the spine is up). It must still get a heartbeat watcher —
    2026-08-23 finding 1: every production actuator had watch_task=None."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        await sup.start()  # zero actuators, mirrors app.run() ordering

        actuator = FakeActuator("late", OFF)
        sup.register(_registration("late", heartbeat_timeout_s=0.05), actuator)
        # Drive it out of safe state so the trip is observable.
        await sup.apply(_command("late", on=True))
        await asyncio.sleep(0.15)  # > heartbeat_timeout_s with no beats

        assert any(e.reason == "heartbeat_timeout" and e.actuator_id == "late" for e in rec.events)
        assert actuator.is_safe()
        await sup.stop()

    run(scenario)


def test_late_registration_beats_keep_it_alive() -> None:
    """Beats arriving via heartbeat() hold off the trip for a late-registered guard."""

    async def scenario() -> None:
        sup = InterlockSupervisor(on_event=_null_sink)
        await sup.start()

        actuator = FakeActuator("late", OFF)
        sup.register(_registration("late", heartbeat_timeout_s=0.1), actuator)
        await sup.apply(_command("late", on=True))
        for _ in range(4):
            await asyncio.sleep(0.05)
            sup.heartbeat()

        assert not actuator.is_safe()  # still at commanded level, no trip
        await sup.stop()

    run(scenario)


def test_heartbeat_drill_covers_registry_built_actuators() -> None:
    """The 2026-08-23 review found the drills passing while the protection was
    absent: the drill dummy above registers before start() and gets a
    watcher, while every registry-built actuator registers after and got
    none — every production actuator had watch_task=None. This drill
    replicates the production ordering exactly (app.py: start() first,
    register() from the registry after, beats arriving via the same
    heartbeat() path the spine callback uses) and asserts both the trip and
    the recovery contract: safe on heartbeat loss, and still safe once beats
    resume, until an explicit command says otherwise."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        await sup.start()  # production ordering: spine up, zero actuators yet

        actuator = FakeActuator("prod", OFF)
        sup.register(_registration("prod", heartbeat_timeout_s=0.05), actuator)

        sup.heartbeat()  # engine alive
        assert await sup.apply(_command("prod")) == "applied"
        assert not actuator.is_safe()

        await asyncio.sleep(0.15)  # engine dies: no beats
        assert actuator.is_safe(), "tripped dark"
        assert not sup.is_latched("prod"), "heartbeat loss never latches"
        assert any(e.reason == "heartbeat_timeout" and e.actuator_id == "prod" for e in rec.events)

        sup.heartbeat()  # engine returns
        await asyncio.sleep(0.1)
        assert actuator.is_safe(), "did NOT spring back on"

        assert await sup.apply(_command("prod")) == "applied"
        assert not actuator.is_safe(), "explicit command restores"

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


def test_supervisor_clock_trust_is_settable() -> None:
    """Finding 4: trust was evaluated once at __init__ and frozen — a power
    cut left every command rejected_clock until a manual restart. The
    supervisor's clock trust must be changeable at runtime, not just at
    construction. Inverts test_commands_are_refused_when_the_clock_is_not_trusted's
    fixtures: starts untrusted (refused), then trusts (accepted)."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec, clock_trusted=False)
        actuator = FakeActuator("doser", OFF)
        sup.register(_registration("doser"), actuator)
        await sup.start()
        sup.heartbeat()

        assert await sup.apply(_command("doser")) == "rejected_clock"

        sup.set_clock_trusted(True)

        assert await sup.apply(_command("doser")) == "applied"
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


def test_is_at_safe_state_accessor() -> None:
    """Sibling of ``is_latched``: a read-only, broker-free accessor onto the
    guard's own bookkeeping (``_note_level``'s ``at_safe_state``). Added for
    the 2026-08-23 reconnect-republish fix (app.py's
    ``_republish_safe_states``), which needs to tell "actually tripped dark"
    apart from "still holding a commanded, non-safe level" without safety.py
    taking any dependency on NATS to answer it."""

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        sup.register(_registration(), actuator)
        await sup.start()
        assert sup.is_at_safe_state("ato-pump"), "a freshly started actuator is at safe state"

        await sup.apply(_command("ato-pump", on=True))
        assert not sup.is_at_safe_state("ato-pump"), (
            "a held non-safe command must read as not-at-safe-state"
        )

        await sup.apply(_command("ato-pump", on=False))
        assert sup.is_at_safe_state("ato-pump"), (
            "commanding the declared safe level returns the guard to safe"
        )

        await sup.stop()

    run(scenario)


# ------------------------------------------ drill: a command for nothing we own


def test_a_command_for_an_unknown_actuator_is_refused_not_fatal() -> None:
    """An unregistered actuator_id must be a refusal, never an exception.

    This killed the hub. control-engine published a `led-blue` command from a
    lighting profile; this hardware-io has one probe and zero actuators, so the
    guard lookup raised KeyError straight out of `apply`, up through
    `drain_once`, and out of the process.

    Then it got worse than a crash. BR_CMD is a workqueue, so the command was
    still there on restart: the service came up, fetched the same message, and
    died again. Under systemd's Restart=always that is an unbounded crash loop,
    and the probe publishes nothing for the whole of it. A hub that cannot
    survive being told to move an actuator it does not have is not a hub that
    can be extended, and phase 2 is a second node announcing new actuators.

    Refusing terminates the message, so the queue drains instead of feeding the
    loop forever.
    """

    async def scenario() -> None:
        rec = Recorder()
        sup = InterlockSupervisor(on_event=rec)
        pump = FakeActuator("ato-pump", OFF)
        sup.register(_registration("ato-pump"), pump)
        await sup.start()

        outcome = await sup.apply(_command("led-blue"))
        assert outcome == "rejected_unknown"

        reasons = [e.reason for e in rec.events]
        assert "unknown_actuator" in reasons, (
            "a command for an actuator we do not own has to be audited; silently "
            "dropping it makes a misrouted controller invisible"
        )

        # And the actuator we DO own is untouched. A refusal on one id must not
        # be a side effect on another.
        assert pump.is_safe()

        # The service survives it, which is the whole point.
        assert await sup.apply(_command("ato-pump")) == "applied"

    run(scenario)


# --------------------------------------------------------- drill: driver retry


class FlakyActuator(FakeActuator):
    """Once armed, drive_safe() fails N times, then succeeds.

    Models a transient I2C fault arriving mid-trip — the case a trip exists
    to survive, not just a healthy driver's happy path. Starts unarmed: every
    guard gets one unconditional drive_safe() call inside start() (see
    InterlockSupervisor.start), and a failure there latches the actuator
    before a command can ever move it out of safe state, which would prevent
    the runtime-cap and heartbeat trips these tests exist to exercise from
    ever firing. Arming after start() confines the simulated fault to the
    trip's own retries.
    """

    def __init__(self, actuator_id: str, safe_state: ActuatorLevel, *, failures: int) -> None:
        super().__init__(actuator_id, safe_state)
        self.failures = failures
        self.drive_safe_calls = 0
        self.armed = False

    async def drive_safe(self) -> None:
        if self.armed:
            self.drive_safe_calls += 1
            if self.drive_safe_calls <= self.failures:
                raise OSError("transient I2C fault")
        await super().drive_safe()


def test_runtime_trip_retries_failed_safe_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 2: a transient driver error during a max-runtime trip must not
    leave the actuator latched-but-energised with a dead guard task."""
    monkeypatch.setattr(safety, "RETRY_BACKOFF_S", 0.02)

    async def scenario() -> None:
        rec = Recorder()
        # Heartbeat left at the default (0.17s) is comfortably longer than
        # this scenario needs, so the runtime cap is the only trip in play —
        # no beater task required, unlike test_drill_max_runtime_trips_and_latches.
        actuator = FlakyActuator("flaky", OFF, failures=2)
        sup = InterlockSupervisor(on_event=rec)
        sup.register(_registration("flaky", max_runtime_s=0.05), actuator)
        await sup.start()
        actuator.armed = True
        await sup.apply(_command("flaky", on=True))
        await asyncio.sleep(0.1)  # deadline passes; first two drives fail

        # Retry backoff is patched short above so this settles fast.
        await _wait_until(lambda: actuator.is_safe(), timeout=1.0)

        assert sup.is_latched("flaky")  # the latch stands
        assert actuator.drive_safe_calls >= 3  # it retried
        failed = [e for e in rec.events if not e.reached_safe]
        assert failed  # failures were emitted, not swallowed
        assert any(e.reason == "max_runtime_exceeded" and e.reached_safe for e in rec.events)
        await sup.stop()

    run(scenario)


def test_heartbeat_watcher_survives_drive_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same shape via the heartbeat path: the watcher keeps watching after a
    failed drive, and the actuator lands safe once the driver recovers."""
    monkeypatch.setattr(safety, "RETRY_BACKOFF_S", 0.02)

    async def scenario() -> None:
        actuator = FlakyActuator("flaky", OFF, failures=1)
        sup = InterlockSupervisor(on_event=_null_sink)
        sup.register(_registration("flaky", heartbeat_timeout_s=0.05), actuator)
        await sup.start()
        actuator.armed = True
        await sup.apply(_command("flaky", on=True))

        await _wait_until(lambda: actuator.is_safe(), timeout=1.0)
        assert not sup.is_latched("flaky")  # heartbeat loss does not latch
        await sup.stop()

    run(scenario)


def test_a_failed_success_emit_does_not_trigger_a_spurious_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-08-23 review, fix 1: the success emit inside a trip runs *after*
    guard.driver.drive_safe() already succeeded. A sink that raises there is a
    bookkeeping failure, not a drive failure, and must not re-drive an
    already-safe actuator or file a "drive_safe FAILED" event about a drive
    that never failed.

    Deliberately the heartbeat path, not the runtime-cap one: a max-runtime
    trip's own task is `guard.runtime_task` itself, and _drive_safe's success
    bookkeeping cancels that task — a self-cancellation whose delivery at the
    next real suspension point (the retry sleep) would abort a buggy retry
    loop for reasons that have nothing to do with this fix, masking it. The
    heartbeat watcher's task is never the one _drive_safe cancels, so this
    scenario isolates the actual bug.
    """
    monkeypatch.setattr(safety, "RETRY_BACKOFF_S", 0.02)

    async def scenario() -> None:
        emit_attempts = 0

        async def flaky_sink(event: SafetyEvent) -> None:
            nonlocal emit_attempts
            # Raise exactly once, on the trip's own success event — never on
            # a failure event, and never more than once, so a misclassified
            # retry (the bug) resolves in one extra loop instead of hanging
            # this test on the default RETRY_BACKOFF_S.
            if event.reason == "heartbeat_timeout" and event.reached_safe:
                emit_attempts += 1
                if emit_attempts == 1:
                    raise RuntimeError("sink boom")

        actuator = FakeActuator("ato-pump", OFF)
        sup = InterlockSupervisor(on_event=flaky_sink)
        sup.register(_registration(heartbeat_timeout_s=0.05), actuator)
        await sup.start()
        sup.heartbeat()
        await sup.apply(_command())
        # No further heartbeats: the watcher trips at the declared timeout.
        await asyncio.sleep(0.15)

        assert actuator.is_safe()
        # One drive at startup, one for the trip. A misclassified emit
        # failure would show up here as an extra, spurious re-drive of an
        # actuator that was already safe.
        assert actuator.safe_calls == 2
        await sup.stop()

    run(scenario)


class SlowActuator(FakeActuator):
    """drive_safe() takes real time and tracks whether two calls were ever in
    flight at once.

    A real driver's drive_safe() blocks on the bus for a measurable time (an
    await point a plain raise-or-return fake never exercises) — without one,
    two calls into a synchronous-bodied fake could never truly overlap on a
    single-threaded event loop, which would make the reentrancy race stop()
    must prevent untestable. Proves 2026-08-23 review fix 2.
    """

    def __init__(self, actuator_id: str, safe_state: ActuatorLevel, *, delay_s: float) -> None:
        super().__init__(actuator_id, safe_state)
        self.delay_s = delay_s
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0

    async def drive_safe(self) -> None:
        self.concurrent_calls += 1
        self.max_concurrent_calls = max(self.max_concurrent_calls, self.concurrent_calls)
        try:
            await asyncio.sleep(self.delay_s)
            await super().drive_safe()
        finally:
            self.concurrent_calls -= 1


def test_stop_awaits_a_mid_retry_drive_before_driving_the_guard_itself() -> None:
    """2026-08-23 review, fix 2: a guard's own runtime/watch task can still be
    inside guard.driver.drive_safe() when stop() runs. stop() must wait for
    that call to actually finish before issuing its own drive_safe() on the
    same guard — two invocations racing on one bus is exactly what
    ActuatorDriver makes no reentrancy guarantee about. Also covers the
    paired minor: stop() completes promptly and the actuator ends safe."""

    async def scenario() -> None:
        actuator = SlowActuator("slow", OFF, delay_s=0.08)
        sup = InterlockSupervisor(on_event=_null_sink)
        sup.register(_registration("slow", max_runtime_s=0.02), actuator)
        await sup.start()
        await sup.apply(_command("slow", on=True))

        # The runtime deadline fires ~0.02s after apply and immediately
        # starts a drive_safe() call that will not return until ~0.10s.
        # Stop partway through that window, while the call is still in
        # flight, with margin on both sides against scheduler jitter.
        await asyncio.sleep(0.05)
        t0 = asyncio.get_running_loop().time()
        await sup.stop()
        elapsed = asyncio.get_running_loop().time() - t0

        assert actuator.is_safe()
        assert actuator.max_concurrent_calls == 1
        # Promptly: stop() waits out the in-flight call (up to delay_s) plus
        # its own shutdown drive (another delay_s) and nothing more — not
        # RETRY_BACKOFF_S, not a hang. Comfortably under both with margin.
        assert elapsed < 0.3, f"stop() took {elapsed:.3f}s"

    run(scenario)

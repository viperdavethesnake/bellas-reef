# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Engine service behaviour: the clock-trust gate (PRD host-facts RTC rule)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ActuatorCommand, DeviceAssignment
from bellasreef_control_engine.app import ControlEngine, load_profiles
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from bellasreef_control_engine.publisher import CommandPublisher


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def profile() -> ChannelProfile:
    return ChannelProfile(
        channel_id="led-blue",
        anchor="clock",
        points=(RampPoint(at=time(6), duty=0.0), RampPoint(at=time(18), duty=1.0)),
    )


def _assignment(device_id: str, *, adopted: bool) -> DeviceAssignment:
    """Replicated from tests/test_assignments.py — same shape, no cross-import."""
    kwargs: dict[str, Any] = {}
    if adopted:
        kwargs = {"driver_type": "pi-pwm", "binding": {"channel": "0"}, "role": "light"}
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=adopted,
        **kwargs,
    )


class _FakePublisher(CommandPublisher):
    """A publisher that is always connected and records what it would send.

    Subclasses CommandPublisher rather than duck-typing so ``engine.publisher``
    stays a real ``CommandPublisher | None`` under mypy --strict, and so
    build_pwm_command (pure, no broker touch) is exercised unchanged.
    """

    def __init__(self) -> None:
        super().__init__("nats://unused:4222")
        self.published: list[ActuatorCommand] = []

    @property
    def connected(self) -> bool:
        return True

    async def emit(self, command: ActuatorCommand) -> None:
        self.published.append(command)


@pytest.fixture
def engine_with_fake_publisher() -> tuple[ControlEngine, list[ActuatorCommand]]:
    """A ControlEngine with one channel profile ("led-blue") and a fake spine.

    Nothing is adopted by default — the whole point of the assignment gate is
    that a schedule alone is not enough.
    """
    engine = ControlEngine([profile()], metrics_port=0)
    fake = _FakePublisher()
    engine.publisher = fake
    return engine, fake.published


class TestClockTrustGate:
    """No scheduled emission while the clock is unsynced.

    This board has no RTC battery, so after a power cut the clock is wrong
    until chrony catches up. A command's expires_at comes from that clock.
    """

    def test_no_commands_are_emitted_while_the_clock_is_untrusted(self) -> None:
        async def scenario() -> int:
            engine = ControlEngine([profile()], metrics_port=0)
            engine._clock_trusted = False
            # A tick would emit if the gate were not honoured; there is no
            # publisher, so any attempt is counted as suppressed.
            await engine._tick(datetime(2026, 6, 1, 12, tzinfo=UTC))
            return len(engine.scheduler.due(datetime(2026, 6, 1, 12, tzinfo=UTC)))

        # The scheduler still has an outstanding intent: nothing was published,
        # so nothing was marked emitted.
        assert run(scenario) == 1

    def test_health_is_503_while_the_clock_is_untrusted(self) -> None:
        engine = ControlEngine([profile()], metrics_port=0)
        engine._clock_trusted = False
        health = engine.health()
        assert health.healthy is False
        assert "clock" in health.reason

    def test_losing_clock_trust_resets_emission_history(self) -> None:
        """What was emitted against a clock we no longer believe says nothing
        about what to emit now."""
        engine = ControlEngine([profile()], metrics_port=0)
        now = datetime(2026, 6, 1, 12, tzinfo=UTC)
        intent = engine.scheduler.due(now)[0]
        engine.scheduler.mark_emitted(intent, now)
        assert engine.scheduler.due(now) == []

        # Losing trust resets history; the engine does this in
        # _refresh_clock_trust when the flag flips.
        engine.scheduler.reset()
        assert len(engine.scheduler.due(now)) == 1


class TestAssignmentGate:
    """`_tick` publishes only intents whose channel is adopted (PRD spine plan)."""

    def test_unadopted_channel_is_suppressed_not_published(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """A profile for a channel nobody adopted must produce zero commands."""
        engine, published = engine_with_fake_publisher  # profiles include "led-blue"
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published == []

    def test_adopted_channel_publishes(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, published = engine_with_fake_publisher
        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["led-blue"]

    def test_adoption_mid_run_starts_cold_from_safe_duty(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """Suppressed ticks must not mark_emitted: the first real command after
        adoption is the cold 'initial' intent slewing up from SAFE_DUTY, not a
        mid-ramp jump."""
        engine, published = engine_with_fake_publisher
        asyncio.run(engine._tick(datetime.now(UTC)))  # suppressed
        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published[0].reason == "lighting:initial"

    def test_readoption_after_tombstone_starts_cold_not_from_stale_duty(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """A channel that was adopted, published to, then unadopted, then
        re-adopted must cold-start again — not jump straight to the duty the
        scheduler remembers from before the tombstone.

        hardware-io rebuilds the driver dark on adoption, so a scheduler that
        still remembers the pre-tombstone duty would command a pop from 0 to
        whatever it last emitted, with no slew, the instant a channel is
        re-adopted. Timestamps are spread across the ramp (08:00 -> 08:30 ->
        14:00) so the duty genuinely moves between ticks — this is the "slow"
        path, distinct from test_tombstone_forgets_immediately_even_when_no_tick_is_due
        below, which is the same defect on a tombstone that never appears in
        any tick's due intents at all. Forgetting is driven by the tombstone
        event (AssignmentLedger.on_tombstone), not tick timing, so both are
        covered by the same fix.
        """
        engine, published = engine_with_fake_publisher
        first = datetime(2026, 6, 1, 8, tzinfo=UTC)
        second = datetime(2026, 6, 1, 8, 30, tzinfo=UTC)
        third = datetime(2026, 6, 1, 14, tzinfo=UTC)

        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(first))  # cold "initial" publish
        assert published[0].reason == "lighting:initial"

        engine.assignments.apply(_assignment("led-blue", adopted=False))
        asyncio.run(engine._tick(second))  # tombstoned; suppressed

        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(third))  # re-adopted, hours later on the ramp

        assert published[-1].reason == "lighting:initial"

    def test_tombstone_forgets_immediately_even_when_no_tick_is_due(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """Reproduction from the scoped re-review of this branch's first
        pass at finding 2.

        A forget() called from inside `_tick`'s `for intent in intents:` loop
        only ever runs for a channel `due()` actually surfaces — which
        happens only when it is cold, mid-slew, past the 0.005 deadband, or
        past the 300s refresh window. Unadopt 30s after publish and re-adopt
        30s after that lands well inside both windows: the channel never
        appears in `intents` while suppressed, so a tick-scoped forget()
        never runs at all — not "late", not "throttled", *never*, until
        something else makes the channel due again. The next due tick then
        publishes a "ramp" continuation from the stale pre-tombstone duty:
        the exact pop, on a channel that was dark the whole time in between.

        Forgetting must therefore be driven by the tombstone event itself
        (AssignmentLedger.on_tombstone -> LightingScheduler.forget, wired in
        ControlEngine.__init__), which fires the moment apply() sees
        adopted=False regardless of what any tick is doing — specifically,
        it fires from the ``apply(adopted=False)`` call below, *before*
        ``_tick(unadopt_at)`` ever runs. That is what makes the channel cold
        again at ``readopt_at``: a cold intent bypasses the deadband/refresh
        gates entirely and always emits (see ``due()``), so the very next
        tick where the channel is adopted is already "the next due tick" —
        no need to wait out the 300s refresh window to see the effect.

        Under the tick-scoped forget this replaces, none of that happens:
        ``_last_duty`` is still the pre-tombstone value at ``readopt_at``,
        the delta is inside the deadband and 60s is inside the refresh
        window, so due() does not even surface the channel — nothing
        publishes at ``readopt_at`` at all, and the eventual first
        publish (whenever the schedule next drifts past the deadband or the
        refresh window elapses) is a "ramp" continuation from the stale
        duty, not "initial".
        """
        engine, published = engine_with_fake_publisher
        t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
        unadopt_at = datetime(2026, 6, 1, 8, 0, 30, tzinfo=UTC)
        readopt_at = datetime(2026, 6, 1, 8, 1, 0, tzinfo=UTC)

        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(t0))  # cold "initial" publish
        assert published[0].reason == "lighting:initial"

        # Tombstone 30s later: well inside the scheduler's deadband (0.005)
        # and refresh window (300s). apply() fires on_tombstone here,
        # synchronously, regardless of whether the following tick finds
        # anything due.
        engine.assignments.apply(_assignment("led-blue", adopted=False))
        asyncio.run(engine._tick(unadopt_at))
        assert len(published) == 1, "30s in, well inside deadband/refresh: nothing was due"

        # Re-adopt 30s after that — still well inside both windows by the
        # clock alone. The tombstone already forgot this channel, so it is
        # cold again: this tick is the proof.
        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(readopt_at))

        assert len(published) == 2
        assert published[-1].reason == "lighting:initial"


class TestReconnectReDrain:
    """A NATS reconnect can miss core-subject messages (e.g. a tombstone) sent
    during the gap. `_wire_reconnect_handling` points the publisher's
    `on_reconnected` at `_on_reconnected`, which flips `_assignments_loaded`
    back to False so `_loop`'s existing retry re-drains JetStream — which
    still has whatever was missed.
    """

    def test_wiring_points_the_publisher_at_on_reconnected(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, _ = engine_with_fake_publisher
        assert engine.publisher is not None

        engine._wire_reconnect_handling()

        # Bound-method identity is not stable across attribute reads, so this
        # proves the wiring by effect rather than by inspecting the callable:
        # invoking whatever got wired must produce exactly _on_reconnected's
        # effect. test_reconnect_makes_the_next_loop_iteration_redrain below
        # proves the far end of the same wiring, through the real loop.
        engine._assignments_loaded = True
        assert engine.publisher.on_reconnected is not None
        engine.publisher.on_reconnected()
        assert engine._assignments_loaded is False

    def test_on_reconnected_flips_assignments_loaded_false(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, _ = engine_with_fake_publisher
        engine._assignments_loaded = True

        engine._on_reconnected()

        assert engine._assignments_loaded is False

    def test_wiring_is_a_no_op_with_no_spine(self) -> None:
        """An engine with no publisher (no BELLASREEF_NATS_URL) must not blow
        up when run() calls this unconditionally-safe wiring step."""
        engine = ControlEngine([profile()], metrics_port=0)
        assert engine.publisher is None
        engine._wire_reconnect_handling()  # must not raise

    def test_reconnect_makes_the_next_loop_iteration_redrain(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """End-to-end through the real `_loop`, not a re-statement of its
        condition: after a reconnect, one iteration must call
        load_assignments again and pick up whatever it returns."""
        engine, _ = engine_with_fake_publisher
        assert engine.publisher is not None
        engine._loop_interval_s = 0.0
        engine._assignments_loaded = True
        engine._clock_trusted = False  # keep the iteration to just the redrain check

        redrain_calls = 0

        async def fake_load_assignments(ledger: object) -> bool:
            nonlocal redrain_calls
            redrain_calls += 1
            engine.request_stop()  # stop after the one iteration under test
            return True

        engine.publisher.load_assignments = fake_load_assignments  # type: ignore[method-assign]
        engine._wire_reconnect_handling()

        # Simulates what nats.py does: fires the callback it was handed.
        engine.publisher.on_reconnected()  # type: ignore[misc]
        assert engine._assignments_loaded is False

        asyncio.run(engine._loop())

        assert redrain_calls == 1
        assert engine._assignments_loaded is True


class TestProfileLoading:
    def test_loads_the_shipped_example(self) -> None:
        profiles = load_profiles(Path("deploy/config/lighting.json"))
        assert [p.channel_id for p in profiles] == ["led-blue"]
        assert profiles[0].duty_at(datetime(2026, 6, 1, 13, tzinfo=UTC)) == pytest.approx(1.0)

    def test_an_invalid_profile_raises_rather_than_starting_half_configured(
        self, tmp_path: Path
    ) -> None:
        """Half a schedule would light a tank to a shape nobody designed."""
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps(
                [
                    {
                        "channel_id": "x",
                        "anchor": "clock",
                        "points": [{"at": "08:00:00", "duty": 2.0}],
                    }
                ]
            )
        )
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            load_profiles(bad)

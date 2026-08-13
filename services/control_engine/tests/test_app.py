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

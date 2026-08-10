# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Overrides: persisted deadlines, monotonic within a run, lapse on wake.

Needs a real Postgres — the "single active per target" rule is a partial unique
index, and a mocked store would prove nothing about it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from bellasreef_control_engine.overrides import ActiveOverride, OverrideStore
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from bellasreef_control_engine.scheduler import LightingScheduler
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


async def fresh() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE overrides"))
    return engine


def profile() -> ChannelProfile:
    return ChannelProfile(
        channel_id="blue",
        anchor="clock",
        points=(RampPoint(at=time(6), duty=0.0), RampPoint(at=time(12), duty=1.0)),
    )


class TestSingleActivePerTarget:
    def test_a_second_override_supersedes_the_first(self) -> None:
        """Pressing "off for 30 minutes" again means the new thirty minutes.

        Rejecting it and making the operator cancel first would be ceremony.
        """

        async def scenario() -> tuple[int, list[str]]:
            engine = await fresh()
            store = OverrideStore(engine)
            await store.create("blue", 0.0, 1800, reason="feed")
            await store.create("blue", 0.0, 1800, reason="feed again")
            active = await store.active_count("blue")
            async with engine.connect() as conn:
                reasons = [
                    str(r[0])
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT release_reason FROM overrides WHERE released_at IS NOT NULL"
                            )
                        )
                    ).all()
                ]
            await engine.dispose()
            return active, reasons

        active, reasons = run(scenario)
        assert active == 1, "the partial unique index allows exactly one live override"
        assert reasons == ["superseded"]

    def test_different_targets_do_not_collide(self) -> None:
        async def scenario() -> tuple[int, int]:
            engine = await fresh()
            store = OverrideStore(engine)
            await store.create("blue", 0.0, 600)
            await store.create("white", 0.0, 600)
            counts = (await store.active_count("blue"), await store.active_count("white"))
            await engine.dispose()
            return counts

        assert run(scenario) == (1, 1)


class TestLapseOnWake:
    def test_an_override_that_expired_while_down_lapses_and_is_not_applied(self) -> None:
        """The down-past-expiry case.

        The operator asked for thirty minutes. Coming back to a dark tank hours
        later because the engine restarted is not that — an override that
        outlives its promise is a silent trap.
        """

        async def scenario() -> tuple[list[ActiveOverride], list[str]]:
            engine = await fresh()
            store = OverrideStore(engine)

            # Placed an hour ago for 30 minutes: expired 30 minutes before wake.
            an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
            await store.create("blue", 0.0, 1800, reason="feed", now=an_hour_ago)

            live = await store.load_active()

            async with engine.connect() as conn:
                reasons = [
                    str(r[0])
                    for r in (
                        await conn.execute(text("SELECT release_reason FROM overrides"))
                    ).all()
                ]
            await engine.dispose()
            return live, reasons

        live, reasons = run(scenario)
        assert live == [], "an override past its deadline must not be re-armed"
        assert reasons == ["lapsed"]

    def test_an_override_still_owed_survives_a_restart(self) -> None:
        async def scenario() -> list[ActiveOverride]:
            engine = await fresh()
            store = OverrideStore(engine)
            # Placed a minute ago for an hour: 59 minutes still owed.
            await store.create("blue", 0.0, 3600, now=datetime.now(UTC) - timedelta(minutes=1))
            live = await store.load_active()
            await engine.dispose()
            return live

        live = run(scenario)
        assert len(live) == 1
        assert live[0].target == "blue"
        assert live[0].monotonic_deadline is not None, "a re-armed override must be monotonic"

    def test_reload_is_idempotent(self) -> None:
        """Two wakes in a row must not lapse a live override."""

        async def scenario() -> tuple[int, int]:
            engine = await fresh()
            store = OverrideStore(engine)
            await store.create("blue", 0.0, 3600)
            first = len(await store.load_active())
            second = len(await store.load_active())
            await engine.dispose()
            return first, second

        assert run(scenario) == (1, 1)


class TestMonotonicWithinARun:
    def test_expiry_uses_the_monotonic_deadline_once_armed(self) -> None:
        """A wall-clock jump must not shorten or extend an override.

        chrony steps this board's clock after a power cut; an override that
        changed length because NTP corrected the time would be a surprise on a
        tank.
        """
        now = datetime.now(UTC)
        override = ActiveOverride(
            id=__import__("uuid").uuid4(),
            target="blue",
            duty=0.0,
            expires_at=now + timedelta(seconds=60),
        )
        override.arm(monotonic_now=1000.0, wall_now=now)
        assert override.monotonic_deadline == pytest.approx(1060.0)

        # Wall clock leaps an hour forward; the override is still owed.
        assert override.is_expired(monotonic_now=1030.0, wall_now=now + timedelta(hours=1)) is False
        # And it ends on elapsed seconds, not on the wall.
        assert override.is_expired(monotonic_now=1061.0, wall_now=now) is True

    def test_before_arming_it_falls_back_to_wall_clock(self) -> None:
        """The restart path: monotonic origin died with the old process."""
        now = datetime.now(UTC)
        override = ActiveOverride(
            id=__import__("uuid").uuid4(),
            target="blue",
            duty=0.0,
            expires_at=now - timedelta(seconds=1),
        )
        assert override.monotonic_deadline is None
        assert override.is_expired(wall_now=now) is True


class TestSchedulerIntegration:
    def test_an_override_outranks_the_schedule(self) -> None:
        s = LightingScheduler([profile()], deadband=0.0)
        noon = datetime(2026, 6, 1, 12, tzinfo=UTC)
        assert s.due(noon)[0].duty == pytest.approx(1.0)
        assert s.due(noon, {"blue": 0.0})[0].duty == pytest.approx(0.0)

    def test_release_slews_back_to_the_schedule(self) -> None:
        """§5: override release is one of the three slew causes.

        The tank should not see a pop when a feed-mode hold ends.
        """
        s = LightingScheduler([profile()], deadband=0.0, max_duty_delta_per_s=0.01)
        noon = datetime(2026, 6, 1, 12, tzinfo=UTC)

        # Held dark; converge onto the override first.
        held = noon
        for _ in range(5):
            for intent in s.due(held, {"blue": 0.0}):
                s.mark_emitted(intent, held)
            held = held + timedelta(seconds=10)

        released = s.due(held)[0]
        assert released.duty < 0.2, "release must slew, not jump to full sun"
        assert released.reason == "converge"


class TestValidation:
    @pytest.mark.parametrize(("duty", "duration"), [(0.0, 0.0), (0.0, -1.0), (1.5, 60.0)])
    def test_invalid_overrides_are_refused(self, duty: float, duration: float) -> None:
        async def scenario() -> None:
            engine = await fresh()
            store = OverrideStore(engine)
            try:
                await store.create("blue", duty, duration)
            finally:
                await engine.dispose()

        with pytest.raises(ValueError):
            run(scenario)

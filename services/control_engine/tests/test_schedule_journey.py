# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The "it fires" journey (Task 8): create a curve, assign it, watch the
engine loop actually command a channel from it, edit the curve live, hold and
release, unassign, and prove the whole thing is pure.

This is deliberately end-to-end through ``ControlEngine._tick`` — Task 4
already proves ``ScheduleStore.assigned_curves()`` against real Postgres,
Task 7 already proves ``_reload_schedules`` picks up a store edit within one
tick. What neither proves is that the *whole loop*, assembled, turns a curve
sitting in Postgres into a published duty a reefkeeper would see on a meter —
that is the one thing this file is for.

**Slew choice: instant (``max_duty_delta_per_s=None``).** With a configured
slew, the very first ("cold") tick is clamped to ``SAFE_DUTY`` (see
``LightingScheduler._emit_for``: ``dt_s`` is 0 on a channel's first-ever
emission, so ``_limit`` can move it by zero), so a single tick at 10:00 would
publish 0.0, not the interpolated 0.61 the brief's math describes — proving
that would need a second tick after the slew has had time to arrive, which
adds a rate-dependent step to every leg for no extra proof. Instant keeps
every assertion an exact equality against the curve (or override) that is
live at that moment, including the release-to-curve leg, which is the
strongest form of "returns toward curve-at-now, not SAFE_DUTY": duty lands on
it in one tick rather than merely trending toward it. The slew mechanism
itself — arrival, deadband, refresh, convergence steps — is
``test_scheduler.py``'s job, not this file's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ActuatorCommand, DeviceAssignment, ScheduleDefinition
from bellasreef_control_engine.app import ControlEngine
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from bellasreef_control_engine.publisher import CommandPublisher
from bellasreef_control_engine.scheduler import LightingScheduler
from bellasreef_db.overrides import ActiveOverride
from bellasreef_db.schedules import ScheduleStore

CHANNEL = "pi-pwm-0"


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def _assignment(device_id: str, *, adopted: bool) -> DeviceAssignment:
    """Same shape as test_app.py's helper of the same name — a separate file
    per this repo's test convention (no cross-imports between test modules)."""
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
    """Mirrors test_app.py's ``_FakePublisher``: always connected, records
    what it would send, subclassed (not duck-typed) so ``engine.publisher``
    stays a real ``CommandPublisher | None`` under mypy --strict."""

    def __init__(self) -> None:
        super().__init__("nats://unused:4222")
        self.published: list[ActuatorCommand] = []

    @property
    def connected(self) -> bool:
        return True

    async def emit(self, command: ActuatorCommand) -> None:
        self.published.append(command)


class _FakeScheduleStore(ScheduleStore):
    """Mirrors test_app.py's ``_FakeScheduleStore``: assigned curves live in a
    dict, standing in for Postgres — Task 4 already proves the real store."""

    def __init__(self) -> None:
        self.curves: dict[str, ScheduleDefinition] = {}

    async def assigned_curves(self) -> dict[str, ScheduleDefinition]:
        return dict(self.curves)


def _duty(definition: ScheduleDefinition, at: datetime) -> float:
    """Independent oracle for "what should the curve say right now": builds a
    fresh ``ChannelProfile`` straight from the wire definition and asks its
    own ``duty_at`` — the same function ``_reload_schedules`` calls, but
    invoked here rather than inspected there, so the assertion is against the
    curve's own math rather than against whatever the engine happens to hold
    in memory."""
    return ChannelProfile.from_definition(CHANNEL, definition).duty_at(at)


class TestScheduleJourney:
    """create curve -> assign -> fire -> edit -> hold -> release-to-curve ->
    unassign -> purity, all through one running ``ControlEngine``."""

    def test_it_fires(self) -> None:
        store = _FakeScheduleStore()
        engine = ControlEngine([], metrics_port=0, schedule_store=store)
        fake = _FakePublisher()
        engine.publisher = fake
        engine.assignments.apply(_assignment(CHANNEL, adopted=True))

        # ---- create a curve and assign it -----------------------------
        # 35% at 08:00 -> 100% at 13:00, zone defaults to UTC (zone-local).
        curve_v1 = ScheduleDefinition(
            name="reef-day",
            points=(RampPoint(at=time(8, 0), duty=0.35), RampPoint(at=time(13, 0), duty=1.0)),
        )
        store.curves = {CHANNEL: curve_v1}

        # ---- tick at 10:00 zone-local: published duty == interpolated ---
        # 2/5 of the way from 08:00 to 13:00: 0.35 + (2/5) x 0.65 = 0.61.
        t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        run(lambda: engine._tick(t0))

        assert [c.actuator_id for c in fake.published] == [CHANNEL]
        first_tick = fake.published[0]
        assert first_tick.level.duty == pytest.approx(0.61)  # type: ignore[union-attr]
        assert first_tick.level.duty == pytest.approx(_duty(curve_v1, t0))  # type: ignore[union-attr]
        assert first_tick.reason == "lighting:initial"
        first_tick_duty = first_tick.level.duty  # type: ignore[union-attr]

        # ---- edit the curve: next tick moves toward the new value -------
        curve_v2 = ScheduleDefinition(
            name="reef-day",
            points=(RampPoint(at=time(8, 0), duty=0.35), RampPoint(at=time(13, 0), duty=0.85)),
        )
        store.curves = {CHANNEL: curve_v2}
        t1 = t0 + timedelta(minutes=5)
        run(lambda: engine._tick(t1))

        edited = fake.published[-1]
        expected_v2_at_t1 = _duty(curve_v2, t1)
        assert edited.level.duty == pytest.approx(expected_v2_at_t1)  # type: ignore[union-attr]
        # The edit actually moved the target — this is not just re-reading v1.
        assert edited.level.duty != pytest.approx(_duty(curve_v1, t1))  # type: ignore[union-attr]
        assert edited.reason == "lighting:ramp"

        # ---- place a ramp hold at 50%: published duty is held -----------
        t2 = t1 + timedelta(minutes=1)
        engine._held[CHANNEL] = ActiveOverride(
            id=uuid4(),
            target=CHANNEL,
            duty=0.5,
            expires_at=t2 + timedelta(hours=1),
            transition="ramp",
        )
        run(lambda: engine._tick(t2))

        held = fake.published[-1]
        assert held.level.duty == pytest.approx(0.5)  # type: ignore[union-attr]
        assert held.reason == "lighting:hold"

        # ---- release: published duty returns toward curve-at-now, NOT
        # SAFE_DUTY -- the resting-state proof the hold spec deferred. ------
        del engine._held[CHANNEL]
        t3 = t2 + timedelta(minutes=1)
        run(lambda: engine._tick(t3))

        released = fake.published[-1]
        expected_resting_at_t3 = _duty(curve_v2, t3)
        assert expected_resting_at_t3 != pytest.approx(0.0)
        assert released.level.duty == pytest.approx(expected_resting_at_t3)  # type: ignore[union-attr]
        assert released.level.duty != pytest.approx(0.0)  # type: ignore[union-attr]
        assert released.reason == "lighting:ramp"

        # ---- unassign: converges to 0.0 ----------------------------------
        store.curves = {}
        t4 = t3 + timedelta(minutes=1)
        run(lambda: engine._tick(t4))

        unassigned = fake.published[-1]
        assert unassigned.actuator_id == CHANNEL
        assert unassigned.level.duty == pytest.approx(0.0)  # type: ignore[union-attr]
        settled_count = len(fake.published)

        # Converged and resting at SAFE_DUTY: goes quiet, no further commands.
        t5 = t4 + timedelta(minutes=1)
        run(lambda: engine._tick(t5))
        assert len(fake.published) == settled_count

        # ---- purity: repeat the first tick with identical state and ------
        # assert an identical answer. Fresh store, fresh engine, fresh
        # publisher -- none of this test's mutation history (the edit, the
        # hold, the unassign) is reachable from here, so an identical answer
        # is proof of purity, not of shared state.
        store2 = _FakeScheduleStore()
        store2.curves = {CHANNEL: curve_v1}
        engine2 = ControlEngine([], metrics_port=0, schedule_store=store2)
        fake2 = _FakePublisher()
        engine2.publisher = fake2
        engine2.assignments.apply(_assignment(CHANNEL, adopted=True))

        run(lambda: engine2._tick(t0))

        assert len(fake2.published) == 1
        assert fake2.published[0].level.duty == pytest.approx(first_tick_duty)  # type: ignore[union-attr]
        assert fake2.published[0].reason == first_tick.reason


class TestFireTimeTable:
    """``due()`` emits the exact interpolated duty at three clocks spanning
    the midnight wrap -- the segment ``profiles.py`` computes by wrapping
    from the last point back to the first rather than flattening it (see
    ``ChannelProfile.duty_at``'s docstring).

    Curve: 20% at 06:00, 80% at 18:00. Two 12-hour segments at the same rate,
    so both the normal segment and the wrap segment interpolate linearly and
    the expected values below are exact fractions, not approximations of a
    ratio.

    Each row builds its own scheduler so every call to ``due()`` is a cold
    start: that bypasses the deadband, which a shared scheduler could
    otherwise use to silently swallow a row whose target happened to land
    close to the previous one (irrelevant to the interpolation this test
    is checking).
    """

    _POINTS = (RampPoint(at=time(6, 0), duty=0.2), RampPoint(at=time(18, 0), duty=0.8))

    @pytest.mark.parametrize(
        ("clock", "expected_duty"),
        [
            # Normal segment, exact midpoint of 06:00 -> 18:00.
            (datetime(2026, 6, 1, 12, 0, tzinfo=UTC), 0.5),
            # Wrap segment: 3h past 18:00, descending toward 06:00's 0.2.
            (datetime(2026, 6, 1, 21, 0, tzinfo=UTC), 0.65),
            # Wrap segment: before 06:00, still descending from 18:00's 0.8.
            (datetime(2026, 6, 1, 3, 0, tzinfo=UTC), 0.35),
        ],
    )
    def test_due_emits_the_interpolated_duty_across_the_wrap(
        self, clock: datetime, expected_duty: float
    ) -> None:
        profile = ChannelProfile(channel_id=CHANNEL, anchor="clock", points=self._POINTS)
        scheduler = LightingScheduler([profile], max_duty_delta_per_s=None)

        intents = scheduler.due(clock)

        assert len(intents) == 1
        assert intents[0].channel_id == CHANNEL
        assert intents[0].duty == pytest.approx(expected_duty)

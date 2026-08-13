# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Scheduler decisions, against fixed clocks."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from bellasreef_control_engine.scheduler import LightingScheduler


def ramp(channel: str = "blue") -> ChannelProfile:
    return ChannelProfile(
        channel_id=channel,
        anchor="clock",
        points=(
            RampPoint(at=time(6), duty=0.0),
            RampPoint(at=time(12), duty=1.0),
            RampPoint(at=time(22), duty=0.0),
        ),
    )


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 6, 1, hour, minute, second, tzinfo=UTC)


class TestEmissionPolicy:
    def test_first_tick_always_emits(self) -> None:
        """A restarted engine must state the level, not assume hardware knows."""
        s = LightingScheduler([ramp()])
        intents = s.due(at(9))
        assert [i.reason for i in intents] == ["initial"]

    def test_a_small_change_inside_the_deadband_is_not_worth_a_message(self) -> None:
        s = LightingScheduler([ramp()], deadband=0.05, refresh_s=3600)
        first = s.due(at(9))[0]
        s.mark_emitted(first, at(9))
        assert s.due(at(9, 0, 30)) == []

    def test_a_change_past_the_deadband_emits(self) -> None:
        s = LightingScheduler([ramp()], deadband=0.01, refresh_s=3600)
        first = s.due(at(9))[0]
        s.mark_emitted(first, at(9))
        intents = s.due(at(9, 30))
        assert [i.reason for i in intents] == ["ramp"]

    def test_refresh_restates_an_unchanged_level(self) -> None:
        """Flat midday still gets restated, so a restarted consumer learns it."""
        s = LightingScheduler([ramp()], deadband=0.5, refresh_s=60)
        first = s.due(at(12))[0]
        s.mark_emitted(first, at(12))
        assert s.due(at(12, 0, 30)) == []
        assert [i.reason for i in s.due(at(12, 2))] == ["refresh"]

    def test_due_is_pure(self) -> None:
        """Calling due() must not record anything.

        Only mark_emitted() does, and only for intents that were actually
        published — otherwise a failed publish silently skips the next one.
        """
        s = LightingScheduler([ramp()])
        assert s.due(at(9)) == s.due(at(9))
        assert len(s.due(at(9))) == 1

    def test_a_failed_publish_leaves_the_intent_outstanding(self) -> None:
        s = LightingScheduler([ramp()], deadband=0.01)
        s.due(at(9))  # decided, but publish "failed" so no mark_emitted
        assert [i.reason for i in s.due(at(9))] == ["initial"]


class TestMultiChannel:
    def test_channels_are_scheduled_independently(self) -> None:
        blue = ramp("blue")
        white = ChannelProfile(
            channel_id="white",
            anchor="clock",
            points=(RampPoint(at=time(8), duty=0.0), RampPoint(at=time(18), duty=1.0)),
        )
        s = LightingScheduler([blue, white])
        intents = {i.channel_id: i.duty for i in s.due(at(13))}
        assert set(intents) == {"blue", "white"}
        assert intents["blue"] != pytest.approx(intents["white"])

    def test_duplicate_channel_ids_are_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate channel_id"):
            LightingScheduler([ramp("blue"), ramp("blue")])


class TestClockTrustReset:
    def test_reset_forgets_history(self) -> None:
        """After a clock-trust gap, what we emitted against a clock we no
        longer believe tells us nothing about what to emit now."""
        s = LightingScheduler([ramp()], deadband=0.5, refresh_s=3600)
        first = s.due(at(12))[0]
        s.mark_emitted(first, at(12))
        assert s.due(at(12, 1)) == []

        s.reset()
        assert [i.reason for i in s.due(at(12, 1))] == ["initial"]


class TestForget:
    def test_forget_clears_one_channel_only(self) -> None:
        """The re-adoption pop: a channel torn down and rebuilt dark must
        cold-start again, not resume from what the scheduler last emitted —
        and forgetting one channel must not disturb another's history."""
        blue = ramp("blue")
        white = ChannelProfile(
            channel_id="white",
            anchor="clock",
            points=(RampPoint(at=time(8), duty=0.0), RampPoint(at=time(18), duty=1.0)),
        )
        s = LightingScheduler([blue, white], deadband=0.5, refresh_s=3600)
        for intent in s.due(at(12)):
            s.mark_emitted(intent, at(12))
        assert s.due(at(12, 1)) == []

        s.forget("blue")
        reasons = {i.channel_id: i.reason for i in s.due(at(12, 1))}
        assert reasons["blue"] == "initial"
        assert "white" not in reasons

    def test_forget_an_unknown_channel_is_a_no_op(self) -> None:
        s = LightingScheduler([ramp()])
        s.forget("no-such-channel")  # must not raise


class TestValidation:
    @pytest.mark.parametrize(("deadband", "refresh"), [(-0.1, 60.0), (0.01, 0.0)])
    def test_invalid_tuning_is_refused(self, deadband: float, refresh: float) -> None:
        with pytest.raises(ValueError):
            LightingScheduler([ramp()], deadband=deadband, refresh_s=refresh)

    def test_naive_clock_is_refused(self) -> None:
        s = LightingScheduler([ramp()])
        with pytest.raises(ValueError, match="timezone-aware"):
            s.due(datetime(2026, 6, 1, 9, 0))  # noqa: DTZ001


class TestRampAcrossTheBand:
    def test_the_engine_publishes_sub_eight_percent_intents(self) -> None:
        """Snap-to-0 belongs to the driver. The engine must say what it means.

        Walking dawn minute by minute, some intents land inside the XLG's
        undefined 0-8% band. That is correct: the engine reports the schedule,
        and the PCA9685 driver decides what the hardware can do with it.
        """
        s = LightingScheduler([ramp()], deadband=0.0005, refresh_s=3600)
        seen: list[float] = []
        for minute in range(0, 60):
            for intent in s.due(at(6, minute)):
                seen.append(intent.duty)
                s.mark_emitted(intent, at(6, minute))

        in_band = [d for d in seen if 0.0 < d < 0.08]
        assert in_band, "dawn ramp produced no sub-8% intents"
        assert max(in_band) < 0.08

    def test_the_band_is_crossed_twice_a_day(self) -> None:
        profile = ramp()
        dawn = [profile.duty_at(at(6, m)) for m in range(60)]
        dusk = [profile.duty_at(at(21, m)) for m in range(60)]
        assert any(0.0 < d < 0.08 for d in dawn)
        assert any(0.0 < d < 0.08 for d in dusk)


def test_refresh_uses_the_publish_time_not_the_decision_time() -> None:
    """mark_emitted records when it went out, so refresh intervals are honest."""
    s = LightingScheduler([ramp()], deadband=0.9, refresh_s=100)
    intent = s.due(at(12))[0]
    published_at = at(12) + timedelta(seconds=30)
    s.mark_emitted(intent, published_at)
    assert s.due(published_at + timedelta(seconds=99)) == []
    assert s.due(published_at + timedelta(seconds=101)) != []

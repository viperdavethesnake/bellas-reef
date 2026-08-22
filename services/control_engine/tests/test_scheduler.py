# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Scheduler decisions, against fixed clocks."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from bellasreef_control_engine.scheduler import HeldTarget, LightingScheduler


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


PROFILE_LED_BLUE = ramp("led-blue")
T0 = at(9)
T1 = at(9, 0, 5)
T2 = at(9, 0, 10)


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


class TestHeldUnprofiledChannels:
    """A held override on an adopted-but-unprofiled channel (every channel
    adopted through the app, spec 2026-08-15) behaves as a constant-SAFE_DUTY
    schedule the override outranks."""

    def test_held_unprofiled_channel_is_emitted(self) -> None:
        sched = LightingScheduler([], max_duty_delta_per_s=None)  # no profiles
        intents = sched.due(T0, {"pi-pwm-0": HeldTarget(0.5, "ramp")})
        assert [(i.channel_id, i.duty) for i in intents] == [("pi-pwm-0", 0.5)]

    def test_held_unprofiled_channel_slews_from_safe_start(self) -> None:
        # with a slew rate configured, the first emission climbs from
        # SAFE_DUTY toward the held duty rather than popping to it
        sched = LightingScheduler([], max_duty_delta_per_s=0.1)
        first = sched.due(T0, {"pi-pwm-0": HeldTarget(0.5, "ramp")})
        assert first and first[0].duty < 0.5  # converging, not popped

    def test_release_slews_back_to_safe_and_goes_quiet(self) -> None:
        sched = LightingScheduler([], max_duty_delta_per_s=None)
        held = sched.due(T0, {"pi-pwm-0": HeldTarget(0.5, "ramp")})
        sched.mark_emitted(held[0], T0)
        # override gone: target falls to SAFE_DUTY
        released = sched.due(T1, {})
        assert [(i.channel_id, i.duty) for i in released] == [("pi-pwm-0", 0.0)]
        sched.mark_emitted(released[0], T1)
        # converged at 0: nothing further
        assert sched.due(T2, {}) == []

    def test_profiled_channels_unaffected_by_held_strangers(self) -> None:
        # a profile plus a held stranger: both emit, profile from its own curve
        sched = LightingScheduler([PROFILE_LED_BLUE], max_duty_delta_per_s=None)
        intents = sched.due(T0, {"pi-pwm-0": HeldTarget(0.3, "ramp")})
        ids = {i.channel_id for i in intents}
        assert ids == {"led-blue", "pi-pwm-0"}


def snap(duty: float) -> HeldTarget:
    return HeldTarget(duty, "snap")


def ramp_hold(duty: float) -> HeldTarget:
    return HeldTarget(duty, "ramp")


class TestHoldTransition:
    """A target that comes from a hold moves the way that hold says
    (spec 2026-08-17). Slew 0.01/s with ticks 5 s apart: a ramp moves at most
    0.05 per tick, so anything larger in one intent is a snap."""

    def test_snap_hold_arrives_in_one_intent(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [intent] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        assert (intent.duty, intent.reason, intent.hold) == (1.0, "hold", "snap")

    def test_ramp_hold_still_slews(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [intent] = s.due(T0, {"pi-pwm-0": ramp_hold(1.0)})
        assert intent.duty < 1.0
        assert intent.hold == "ramp"

    def test_snap_release_jumps_to_resting_then_goes_quiet(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01, refresh_s=3600)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        [released] = s.due(T1, {})
        assert (released.duty, released.reason, released.hold) == (0.0, "release", None)
        s.mark_emitted(released, T1)
        assert s.due(T2, {}) == []

    def test_ramp_release_still_slews(self) -> None:
        # slew 0.1/s: T0 arrives at 0.0 (dt 0), T1 (+5 s) converges to 0.5,
        # then release 2 s later may move at most 0.2 -> 0.3, reason converge
        s = LightingScheduler([], max_duty_delta_per_s=0.1)
        [a] = s.due(T0, {"pi-pwm-0": ramp_hold(1.0)})
        s.mark_emitted(a, T0)
        [b] = s.due(T1, {"pi-pwm-0": ramp_hold(1.0)})
        assert b.duty == pytest.approx(0.5)
        s.mark_emitted(b, T1)
        [released] = s.due(at(9, 0, 7), {})
        assert (released.reason, released.hold) == ("converge", None)
        assert released.duty == pytest.approx(0.3)

    def test_supersede_ramp_with_snap_jumps(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [first] = s.due(T0, {"pi-pwm-0": ramp_hold(1.0)})
        s.mark_emitted(first, T0)
        [second] = s.due(T1, {"pi-pwm-0": snap(1.0)})
        assert (second.duty, second.reason, second.hold) == (1.0, "hold", "snap")

    def test_supersede_snap_with_ramp_ramps_and_forgets_snap(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01, refresh_s=3600)
        [first] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(first, T0)
        [second] = s.due(T1, {"pi-pwm-0": ramp_hold(0.0)})
        assert second.reason == "hold"  # arrival of the new hold is announced
        assert second.hold == "ramp"
        assert second.duty == pytest.approx(0.95)  # ramping down, not snapping
        s.mark_emitted(second, T1)
        # release now behaves as a ramp release: converge, not a jump
        [released] = s.due(T2, {})
        assert released.reason == "converge"
        assert released.duty == pytest.approx(0.90)

    def test_restart_rearm_honours_snap(self) -> None:
        # cold scheduler (engine restart) with a snap hold already owed
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [intent] = s.due(T0, {"pi-pwm-0": snap(0.7)})
        assert (intent.duty, intent.reason) == (0.7, "hold")

    def test_forget_clears_release_memory(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        s.forget("pi-pwm-0")
        # forgotten and unheld: nothing surfaces, and nothing snap-releases
        assert s.due(T1, {}) == []

    def test_reset_clears_release_memory(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        s.reset()
        assert s.due(T1, {}) == []

    def test_profiled_channel_snap_release_jumps_to_the_curve(self) -> None:
        s = LightingScheduler([ramp()], deadband=0.0, max_duty_delta_per_s=0.01)
        [held] = s.due(at(9), {"blue": snap(1.0)})
        s.mark_emitted(held, at(9))
        [released] = s.due(at(9, 0, 5), {})
        # 09:00 on the 06:00→12:00 ramp is 0.5; jump straight there
        assert released.reason == "release"
        assert released.duty == pytest.approx(0.5, abs=0.001)

    def test_snap_hold_at_target_refreshes_like_any_level(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01, refresh_s=10)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        assert s.due(T1, {"pi-pwm-0": snap(1.0)}) == []
        [again] = s.due(T2, {"pi-pwm-0": snap(1.0)})
        assert (again.reason, again.hold) == ("refresh", "snap")

    def test_due_stays_pure_while_held(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        assert s.due(T0, {"pi-pwm-0": snap(1.0)}) == s.due(T0, {"pi-pwm-0": snap(1.0)})


def test_refresh_uses_the_publish_time_not_the_decision_time() -> None:
    """mark_emitted records when it went out, so refresh intervals are honest."""
    s = LightingScheduler([ramp()], deadband=0.9, refresh_s=100)
    intent = s.due(at(12))[0]
    published_at = at(12) + timedelta(seconds=30)
    s.mark_emitted(intent, published_at)
    assert s.due(published_at + timedelta(seconds=99)) == []
    assert s.due(published_at + timedelta(seconds=101)) != []


class TestSlewArrival:
    """The last step of a slew must land, even when the residual is inside
    the deadband (bench 2026-08-17: a ramp hold to 100 % published 0.9956
    and then nothing — 3.294 V on the meter, "Now 99 %" in the app — until
    the 300 s refresh). The deadband suppresses noise, not arrival."""

    def test_final_slew_step_inside_deadband_still_lands(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)  # default deadband 0.005
        [first] = s.due(T0, {"pi-pwm-0": ramp_hold(1.0)})
        s.mark_emitted(first, T0)
        t_almost = T0 + timedelta(seconds=99.56)
        [almost] = s.due(t_almost, {"pi-pwm-0": ramp_hold(1.0)})
        assert almost.duty == pytest.approx(0.9956)
        assert almost.reason == "converge"
        s.mark_emitted(almost, t_almost)
        t_arrive = t_almost + timedelta(seconds=1)
        [arrived] = s.due(t_arrive, {"pi-pwm-0": ramp_hold(1.0)})
        assert (arrived.duty, arrived.reason) == (1.0, "converge")
        s.mark_emitted(arrived, t_arrive)
        # at target: quiet until refresh
        assert s.due(t_arrive + timedelta(seconds=1), {"pi-pwm-0": ramp_hold(1.0)}) == []

    def test_final_step_of_a_release_slew_lands_too(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        # supersede with a ramp hold at 0.0044: one slew step short of dark
        [step] = s.due(T1, {"pi-pwm-0": ramp_hold(0.0)})
        s.mark_emitted(step, T1)  # 0.95, hold
        t = T1
        for _ in range(94):
            t = t + timedelta(seconds=1)
            [i] = s.due(t, {"pi-pwm-0": ramp_hold(0.0)})
            s.mark_emitted(i, t)
        assert i.duty == pytest.approx(0.01)
        # release: 0.01 → 0.0 is 0.01, exactly at the allowance; then nothing
        # must be left short — the channel must reach 0.0 and go quiet
        t = t + timedelta(seconds=1)
        [rel] = s.due(t, {})
        assert rel.duty == pytest.approx(0.0)
        s.mark_emitted(rel, t)
        assert s.due(t + timedelta(seconds=1), {}) == []

    def test_release_to_curve_final_step_inside_deadband_still_lands(self) -> None:
        """Same bug class as the two tests above, aimed at the one leg
        neither covers: a RAMP release back onto a profiled channel's
        non-zero resting duty (0.61, not SAFE_DUTY). Numbers mirror
        test_final_slew_step_inside_deadband_still_lands — the arrival
        step's residual (0.0044) sits inside the default 0.005 deadband, so
        without the arrival bypass this stalls at 0.6056 until refresh."""
        curve = flat("blue", 0.61)
        s = LightingScheduler([curve], max_duty_delta_per_s=0.01)  # default deadband 0.005

        # A ramp hold at 0.0 settles immediately on the very first (cold, dt=0)
        # tick, holding the channel away from the curve's resting duty until
        # it is released.
        [held] = s.due(T0, {"blue": ramp_hold(0.0)})
        assert held.duty == pytest.approx(0.0)
        s.mark_emitted(held, T0)

        t_release = T0 + timedelta(seconds=1)
        [released] = s.due(t_release, {})
        assert released.reason == "converge"
        s.mark_emitted(released, t_release)

        t_almost = t_release + timedelta(seconds=59.56)
        [almost] = s.due(t_almost, {})
        assert almost.duty == pytest.approx(0.6056)
        assert almost.reason == "converge"
        s.mark_emitted(almost, t_almost)

        t_arrive = t_almost + timedelta(seconds=1)
        [arrived] = s.due(t_arrive, {})
        assert arrived.duty == pytest.approx(0.61)
        assert arrived.reason == "converge"
        s.mark_emitted(arrived, t_arrive)

        # at the curve's resting duty: quiet until refresh
        assert s.due(t_arrive + timedelta(seconds=1), {}) == []


def flat(channel: str, duty: float) -> ChannelProfile:
    """A two-point curve that holds one duty all day — the min-length-2 rule
    without needing a real diurnal shape for these tests."""
    return ChannelProfile(
        channel_id=channel,
        anchor="clock",
        points=(RampPoint(at=time(0), duty=duty), RampPoint(at=time(12), duty=duty)),
    )


class TestSetProfiles:
    """set_profiles swaps the schedule set live; emission history is kept on
    purpose (spec: a changed curve is a moved target, not a cold start)."""

    def test_set_profiles_curve_edit_converges_not_jumps(self) -> None:
        v1 = flat("blue", 0.2)
        v2 = flat("blue", 0.8)
        s = LightingScheduler([v1], max_duty_delta_per_s=0.01)
        first = s.due(T0)[0]
        assert first.reason == "initial"
        s.mark_emitted(first, T0)

        # let the cold start fully settle at 0.2 before editing the curve —
        # the first tick itself is dt=0 and slew-limited short of the target,
        # which would confound this test with the cold-start slew instead of
        # the curve-edit slew this test is actually about.
        settle_at = T0 + timedelta(seconds=30)
        settled = s.due(settle_at)[0]
        assert settled.duty == pytest.approx(0.2)
        s.mark_emitted(settled, settle_at)

        s.set_profiles([v2])

        [moved] = s.due(settle_at + timedelta(seconds=5))
        assert moved.reason == "converge"
        # a moved target, converging under the slew — not a jump straight to 0.8
        assert 0.2 < moved.duty < 0.8

    def test_set_profiles_unassign_converges_to_dark(self) -> None:
        s = LightingScheduler([flat("blue", 0.5)], max_duty_delta_per_s=0.1, deadband=0.005)
        first = s.due(T0)[0]
        s.mark_emitted(first, T0)

        # settle at 0.5 first, same reasoning as the curve-edit test above
        settle_at = T0 + timedelta(seconds=10)
        settled = s.due(settle_at)[0]
        assert settled.duty == pytest.approx(0.5)
        s.mark_emitted(settled, settle_at)

        s.set_profiles([])  # dropped: falls into the synthetic constant-SAFE_DUTY path

        t = settle_at
        previous = settled.duty
        for _ in range(6):  # 0.5 at 0.1/s over 1 s steps, plus a float-residual arrival step
            t = t + timedelta(seconds=1)
            [intent] = s.due(t)
            assert intent.duty < previous  # walking down, not popped straight to 0
            previous = intent.duty
            s.mark_emitted(intent, t)

        assert previous == pytest.approx(0.0)
        assert s.due(t + timedelta(seconds=1)) == []  # converged: quiet

    def test_set_profiles_preserves_hold_memory(self) -> None:
        v1 = flat("blue", 0.2)
        v2 = flat("blue", 0.9)
        s = LightingScheduler([v1])
        [held] = s.due(T0, {"blue": snap(1.0)})
        assert (held.duty, held.reason, held.hold) == (1.0, "hold", "snap")
        s.mark_emitted(held, T0)

        s.set_profiles([v2])

        # the hold still outranks: swapping the curve underneath it changes nothing
        assert s.due(T1, {"blue": snap(1.0)}) == []

        # release: snap-returns to the NEW curve's resting value, not v1's
        [released] = s.due(T2, {})
        assert (released.reason, released.hold) == ("release", None)
        assert released.duty == pytest.approx(0.9)

    def test_set_profiles_duplicate_channel_rejected(self) -> None:
        s = LightingScheduler([ramp("blue")])
        with pytest.raises(ValueError, match="duplicate channel_id"):
            s.set_profiles([ramp("blue"), ramp("blue")])

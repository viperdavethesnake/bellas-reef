# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""docs/contracts/time-and-scheduling.md v1 items.

Schema-now fields, converge-with-slew on restart, and the slew knob.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from bellasreef_control_engine.profiles import ChannelProfile, Locale, RampPoint
from bellasreef_control_engine.scheduler import SAFE_DUTY, LightingScheduler
from pydantic import ValidationError


def profile(**over: object) -> ChannelProfile:
    base: dict[str, object] = {
        "channel_id": "blue",
        "anchor": "clock",
        "points": (
            RampPoint(at=time(6), duty=0.0),
            RampPoint(at=time(12), duty=1.0),
            RampPoint(at=time(22), duty=0.0),
        ),
    }
    base.update(over)
    return ChannelProfile.model_validate(base)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 6, 1, hour, minute, second, tzinfo=UTC)


class TestAnchor:
    def test_clock_is_the_implemented_anchor(self) -> None:
        assert profile().anchor == "clock"

    @pytest.mark.parametrize("anchor", ["solar_natural", "solar_custom"])
    def test_solar_anchors_are_rejected_with_a_pointer_to_v2(self, anchor: str) -> None:
        """Reserved in the schema, refused in v1.

        Accepting a solar anchor and quietly behaving like `clock` would be
        worse than refusing: the operator would get a plausible-looking
        schedule that was not the one they asked for, and nothing would say so.
        """
        with pytest.raises(ValidationError, match="lighting v2"):
            profile(anchor=anchor, locale={"name": "Bora Bora", "lat": -16.5, "lon": -151.7})

    def test_an_unknown_anchor_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            profile(anchor="vibes")

    def test_anchor_is_required(self) -> None:
        """The whole point of schema-now: a profile written today already has
        the field, so v2 is an addition rather than a migration."""
        with pytest.raises(ValidationError):
            ChannelProfile.model_validate(
                {
                    "channel_id": "blue",
                    "points": [
                        {"at": "06:00:00", "duty": 0.0},
                        {"at": "12:00:00", "duty": 1.0},
                    ],
                }
            )


class TestLocale:
    def test_a_locale_on_a_clock_profile_is_refused(self) -> None:
        """A setting that silently does nothing is a bug report waiting."""
        with pytest.raises(ValidationError, match="only meaningful with a solar anchor"):
            profile(locale={"name": "Bora Bora", "lat": -16.5, "lon": -151.7})

    def test_locale_coordinates_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            Locale(name="nowhere", lat=91.0, lon=0.0)
        with pytest.raises(ValidationError):
            Locale(name="nowhere", lat=0.0, lon=181.0)

    def test_a_valid_locale_round_trips(self) -> None:
        loc = Locale(name="Great Barrier Reef", lat=-18.3, lon=147.7)
        assert loc.lat == pytest.approx(-18.3)


class TestOnMiss:
    def test_lighting_defaults_to_converge(self) -> None:
        """A ramp is state, not events."""
        assert profile().on_miss == "converge"

    def test_skip_is_expressible(self) -> None:
        assert profile(on_miss="skip").on_miss == "skip"

    def test_an_unknown_policy_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            profile(on_miss="guess")


class TestSlewLimiting:
    def test_without_a_slew_the_engine_jumps_to_target(self) -> None:
        s = LightingScheduler([profile()], deadband=0.0)
        assert s.due(at(12))[0].duty == pytest.approx(1.0)

    def test_a_cold_start_converges_from_dark_rather_than_slamming(self) -> None:
        """The headline case: restart at midday, full sun in the profile.

        Without slew the first command would be 0 -> 100% in one step over
        livestock. With it, the engine walks up.
        """
        s = LightingScheduler([profile()], deadband=0.0, max_duty_delta_per_s=0.01)
        first = s.due(at(12))[0]
        assert first.duty == pytest.approx(SAFE_DUTY), (
            "the first emission after a cold start must begin at the safe state"
        )
        assert first.reason == "initial"

    def test_convergence_walks_up_at_the_configured_rate(self) -> None:
        s = LightingScheduler([profile()], deadband=0.0, max_duty_delta_per_s=0.01)
        now = at(12)
        first = s.due(now)[0]
        s.mark_emitted(first, now)

        # 10 s later, at 0.01/s, no more than 0.10 of travel.
        now2 = now + timedelta(seconds=10)
        second = s.due(now2)[0]
        assert second.duty == pytest.approx(0.10, abs=1e-6)
        assert second.reason == "converge"

    def test_convergence_completes_and_then_stops_converging(self) -> None:
        s = LightingScheduler([profile()], deadband=0.0, max_duty_delta_per_s=0.5)
        now = at(12)
        intent = s.due(now)[0]
        s.mark_emitted(intent, now)

        now = now + timedelta(seconds=10)  # 0.5/s * 10 s covers the whole range
        intent = s.due(now)[0]
        # Compare against the target at that instant, not against 1.0: the
        # profile peaks at noon and is already descending ten seconds later.
        assert intent.duty == pytest.approx(profile().duty_at(now))
        assert intent.reason != "converge"

    def test_a_mid_ramp_restart_converges_rather_than_stepping(self) -> None:
        """The restart path from §3, end to end.

        Run the dawn ramp up to 09:00, then wipe state as a restart would, and
        assert the engine walks back to the target instead of jumping to it.
        """
        s = LightingScheduler([profile()], deadband=0.0, max_duty_delta_per_s=0.02)
        now = at(6)
        for minute in range(0, 180, 5):
            step = now + timedelta(minutes=minute)
            for intent in s.due(step):
                s.mark_emitted(intent, step)

        mid_ramp = s.due(at(9))[0].duty
        assert mid_ramp > 0.3, "precondition: the ramp is well underway"

        s.reset()  # the restart

        resumed = s.due(at(9))[0]
        assert resumed.duty == pytest.approx(SAFE_DUTY), "restart must not jump to target"
        assert resumed.reason == "initial"

        # ...and then converges toward where the schedule actually is.
        walked = at(9)
        last = resumed
        s.mark_emitted(last, walked)
        for _ in range(40):
            walked = walked + timedelta(seconds=5)
            intents = s.due(walked)
            if not intents:
                break
            last = intents[0]
            s.mark_emitted(last, walked)

        target_now = profile().duty_at(walked)
        assert last.duty == pytest.approx(target_now, abs=0.02), (
            "convergence should have caught up with the schedule"
        )

    def test_convergence_is_not_stalled_by_the_deadband(self) -> None:
        """A slew step smaller than the deadband must still be emitted.

        Otherwise a slow rate plus a coarse deadband would leave a channel
        parked short of its target forever — the deadband is there to suppress
        noise, not progress.
        """
        s = LightingScheduler([profile()], deadband=0.5, refresh_s=3600, max_duty_delta_per_s=0.001)
        now = at(12)
        first = s.due(now)[0]
        s.mark_emitted(first, now)

        now2 = now + timedelta(seconds=1)  # 0.001 of travel, far under the deadband
        intents = s.due(now2)
        assert intents, "convergence stalled inside the deadband"
        assert intents[0].reason == "converge"

    def test_the_slew_rate_is_validated(self) -> None:
        with pytest.raises(ValueError, match="max_duty_delta_per_s"):
            LightingScheduler([profile()], max_duty_delta_per_s=0.0)
        with pytest.raises(ValueError, match="max_duty_delta_per_s"):
            LightingScheduler([profile()], max_duty_delta_per_s=-1.0)

    def test_slew_limits_downward_moves_too(self) -> None:
        """Dusk is a ramp as much as dawn is."""
        s = LightingScheduler([profile()], deadband=0.0, max_duty_delta_per_s=0.01)
        now = at(12)
        s.mark_emitted(s.due(now)[0], now)
        # Pretend we are at full and the target has dropped to dark.
        s._last_duty["blue"] = 1.0
        s._last_emitted_at["blue"] = at(23)
        intent = s.due(at(23, 0, 10))[0]
        assert intent.duty < 1.0
        assert intent.duty == pytest.approx(0.9, abs=1e-6)

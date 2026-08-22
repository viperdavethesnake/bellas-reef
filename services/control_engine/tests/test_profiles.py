# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Profile maths, against fixed clocks.

Wall time never appears here. A schedule test that depended on when it ran
would be a schedule test you could not trust at 3 a.m. in March.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from bellasreef_contracts.schedules import ScheduleDefinition, SchedulePoint
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from pydantic import ValidationError


def profile(*points: tuple[str, float], zone: str = "UTC", channel: str = "blue") -> ChannelProfile:
    return ChannelProfile(
        channel_id=channel,
        anchor="clock",
        zone=zone,
        points=tuple(RampPoint(at=time.fromisoformat(t), duty=d) for t, d in points),
    )


def at(iso: str, zone: str = "UTC") -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(zone))


class TestInterpolation:
    def test_exact_points_return_their_duty(self) -> None:
        p = profile(("08:00", 0.0), ("12:00", 1.0), ("20:00", 0.0))
        assert p.duty_at(at("2026-06-01T08:00")) == pytest.approx(0.0)
        assert p.duty_at(at("2026-06-01T12:00")) == pytest.approx(1.0)

    def test_midpoint_of_a_ramp(self) -> None:
        p = profile(("08:00", 0.0), ("12:00", 1.0), ("20:00", 0.0))
        assert p.duty_at(at("2026-06-01T10:00")) == pytest.approx(0.5)

    def test_descending_ramp(self) -> None:
        p = profile(("08:00", 0.0), ("12:00", 1.0), ("20:00", 0.0))
        assert p.duty_at(at("2026-06-01T16:00")) == pytest.approx(0.5)

    def test_night_wraps_across_midnight(self) -> None:
        """The wrap segment must interpolate, not flatten.

        Flattening would put a step at the darkest hour, which is when a
        discontinuity is least wanted and most obvious.
        """
        p = profile(("08:00", 0.2), ("20:00", 0.6))
        # 12h of night from 20:00 to 08:00; midnight is 4h in, so 1/3 of the way
        # from 0.6 down to 0.2.
        assert p.duty_at(at("2026-06-02T00:00")) == pytest.approx(0.6 + (0.2 - 0.6) / 3)

    def test_duty_never_leaves_zero_to_one(self) -> None:
        p = profile(("00:00", 0.0), ("12:00", 1.0))
        for hour in range(24):
            duty = p.duty_at(at(f"2026-06-01T{hour:02d}:00"))
            assert 0.0 <= duty <= 1.0


class TestTheEightPercentBand:
    """Snap-to-0 is the driver's job. The engine must command the real value.

    If the engine pre-clamped, the driver's floor would be untestable and the
    schedule would be lying about what it asked for.
    """

    def test_engine_emits_duties_inside_the_undefined_band(self) -> None:
        p = profile(("06:00", 0.0), ("07:00", 1.0))
        # 06:00 -> 07:00 is a full ramp, so ~5 minutes in is ~8%.
        band = [p.duty_at(at(f"2026-06-01T06:0{m}:00")) for m in range(1, 6)]
        assert any(0.0 < d < 0.08 for d in band), (
            "the ramp should pass through the sub-8% band; the engine must not "
            "clamp it away — the PCA9685 driver owns that floor"
        )

    def test_a_dawn_ramp_crosses_the_band_every_day(self) -> None:
        """Not an edge case: dawn and dusk traverse it daily."""
        p = profile(("06:00", 0.0), ("08:00", 1.0), ("20:00", 1.0), ("22:00", 0.0))
        dawn = [p.duty_at(at(f"2026-06-01T06:{m:02d}:00")) for m in range(0, 20)]
        dusk = [p.duty_at(at(f"2026-06-01T21:{m:02d}:00")) for m in range(40, 60)]
        assert any(0.0 < d < 0.08 for d in dawn)
        assert any(0.0 < d < 0.08 for d in dusk)


class TestTimezonePolicy:
    """Both DST behaviours are expressible; the config picks."""

    def test_utc_profile_is_stable_across_a_dst_boundary(self) -> None:
        """Fixed offset: identical photoperiod either side of the change."""
        p = profile(("08:00", 0.0), ("12:00", 1.0), zone="UTC")
        before = p.duty_at(datetime(2026, 3, 7, 10, 0, tzinfo=UTC))
        after = p.duty_at(datetime(2026, 3, 9, 10, 0, tzinfo=UTC))
        assert before == pytest.approx(after)

    def test_civil_time_profile_shifts_against_utc_across_dst(self) -> None:
        """America/Los_Angeles: 10:00 local is a different UTC instant after
        the change, which is exactly the jump the operator is choosing."""
        p = profile(("08:00", 0.0), ("12:00", 1.0), zone="America/Los_Angeles")
        # Same UTC instant, opposite sides of the 2026-03-08 change.
        before = p.duty_at(datetime(2026, 3, 7, 18, 0, tzinfo=UTC))  # 10:00 PST
        after = p.duty_at(datetime(2026, 3, 9, 18, 0, tzinfo=UTC))  # 11:00 PDT
        assert before != pytest.approx(after), (
            "a civil-time profile must move against UTC across a DST change"
        )
        assert before == pytest.approx(0.5)
        assert after == pytest.approx(0.75)

    def test_naive_instants_are_refused(self) -> None:
        p = profile(("08:00", 0.0), ("12:00", 1.0))
        with pytest.raises(ValueError, match="timezone-aware"):
            p.duty_at(datetime(2026, 6, 1, 10, 0))  # noqa: DTZ001


def test_ramp_point_is_the_contracts_model() -> None:
    # One source of truth: the engine's point IS the wire point (spec §5).
    assert RampPoint is SchedulePoint


def test_from_definition_builds_equivalent_profile() -> None:
    d = ScheduleDefinition.model_validate(
        {
            "name": "This One",
            "zone": "America/Los_Angeles",
            "points": [{"at": "08:00", "duty": 0.0}, {"at": "13:00", "duty": 1.0}],
        }
    )
    p = ChannelProfile.from_definition("pi-pwm-0", d)
    assert p.channel_id == "pi-pwm-0"
    assert p.zone == "America/Los_Angeles"
    assert p.points == d.points


class TestValidation:
    def test_unsorted_points_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="ascending"):
            profile(("12:00", 1.0), ("08:00", 0.0))

    def test_duplicate_times_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="same time of day"):
            profile(("08:00", 0.0), ("08:00", 1.0))

    def test_a_single_point_is_not_a_profile(self) -> None:
        with pytest.raises(ValidationError):
            ChannelProfile(
                channel_id="blue", anchor="clock", points=(RampPoint(at=time(8), duty=0.5),)
            )

    def test_unknown_timezone_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="unknown timezone"):
            profile(("08:00", 0.0), ("12:00", 1.0), zone="Mars/Olympus_Mons")

    def test_duty_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            profile(("08:00", 0.0), ("12:00", 1.5))

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Diurnal lighting profiles (PRD R7).

A profile is a set of (time-of-day, duty) points per channel, linearly
interpolated, wrapping across midnight. Sunrise, sunset and a midday peak are
just points; there is no special-cased "sunrise" concept, because a reefkeeper
who wants two peaks or a lunar channel should not need new code.

**Timezone is explicit and per-profile, and that is the DST decision made
expressible rather than made for you.** Profile times are wall-clock in the
named zone:

* ``zone="UTC"`` — fixed offset. The photoperiod is stable year-round and
  sunrise drifts against civil time by an hour twice a year.
* ``zone="America/Los_Angeles"`` — civil time. Sunrise stays at 08:00 on the
  clock, and the photoperiod jumps an hour twice a year.

Corals track light, not clocks, so the first is arguably better husbandry and
the second is what people expect from a schedule. CLAUDE.md still lists the
choice for the reference tank as open; this module supports both and tests
both, so the decision stays a config value rather than a rewrite.

**The engine never clamps duty.** Sub-8% output is undefined on the XLG
drivers, and the ruling is snap-to-0 — but that floor belongs to the PCA9685
driver, which knows the hardware. An engine that pre-clamped would make the
driver's floor untestable and would lie about what the schedule asked for.
"""

from __future__ import annotations

from datetime import datetime, time
from itertools import pairwise
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["ChannelProfile", "RampPoint"]


class RampPoint(BaseModel):
    """One anchor in a profile: at this local time, this duty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: time
    duty: float = Field(ge=0.0, le=1.0)

    @field_validator("at")
    @classmethod
    def _no_microseconds(cls, value: time) -> time:
        # Sub-second precision in a lighting schedule is noise that makes
        # profiles compare unequal for no reason anyone can perceive.
        return value.replace(microsecond=0)

    @property
    def seconds(self) -> int:
        return self.at.hour * 3600 + self.at.minute * 60 + self.at.second


class ChannelProfile(BaseModel):
    """A named PWM channel and its day.

    At least two points are required. One point is not a profile — it is a
    constant, and expressing it as a schedule hides that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    zone: str = "UTC"
    points: tuple[RampPoint, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        try:
            ZoneInfo(self.zone)
        except Exception as exc:
            raise ValueError(f"unknown timezone {self.zone!r}: {exc}") from exc

        seconds = [p.seconds for p in self.points]
        if seconds != sorted(seconds):
            raise ValueError("points must be in ascending time order")
        if len(set(seconds)) != len(seconds):
            raise ValueError("two points share the same time of day")
        return self

    def duty_at(self, instant: datetime) -> float:
        """Interpolated duty for ``instant``.

        ``instant`` must be timezone-aware; it is converted into the profile's
        zone before the time-of-day lookup, so the caller can stay in UTC.
        """
        if instant.tzinfo is None:
            raise ValueError("instant must be timezone-aware")

        local = instant.astimezone(ZoneInfo(self.zone))
        now_s = local.hour * 3600 + local.minute * 60 + local.second

        points = self.points
        first, last = points[0], points[-1]

        # Before the first point or after the last, we are on the segment that
        # wraps midnight. Treating that as flat instead would put a
        # discontinuity at exactly the darkest hour, which is when a step is
        # least wanted and most visible.
        if now_s < first.seconds or now_s >= last.seconds:
            span = (first.seconds + 86400) - last.seconds
            if span == 0:  # pragma: no cover - excluded by the uniqueness check
                return last.duty
            elapsed = (now_s - last.seconds) % 86400
            return _lerp(last.duty, first.duty, elapsed / span)

        for lo, hi in pairwise(points):
            if lo.seconds <= now_s < hi.seconds:
                span = hi.seconds - lo.seconds
                return _lerp(lo.duty, hi.duty, (now_s - lo.seconds) / span)

        return last.duty  # pragma: no cover - unreachable given the guards above


def _lerp(a: float, b: float, t: float) -> float:
    value = a + (b - a) * t
    # Guard the endpoints against float drift; a duty of 1.0000000000000002
    # would fail the contract's `le=1.0` and take a channel out on a rounding
    # error.
    return min(1.0, max(0.0, value))

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Diurnal lighting profiles (PRD R7, docs/contracts/time-and-scheduling.md).

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
the second is what people expect from a schedule. This module supports both
and tests both, so the decision stays a config value rather than a rewrite.

This model is **engine configuration, not a wire contract.** It is versioned
by the config file, so adding a field here does not trigger a
`bellasreef-contracts` MAJOR bump — that rule applies to the wire models.

**The engine never clamps duty.** Sub-8% output is undefined on the XLG
drivers, and the ruling is snap-to-0 — but that floor belongs to the PCA9685
driver, which knows the hardware. An engine that pre-clamped would make the
driver's floor untestable and would lie about what the schedule asked for.
"""

from __future__ import annotations

from datetime import datetime, time
from itertools import pairwise
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["Anchor", "ChannelProfile", "Locale", "OnMiss", "RampPoint"]

#: Where a profile's day *shape* sits in the operator's day.
#:
#: Only ``clock`` is implemented. The solar anchors are schema-now / v2-build
#: per docs/contracts/time-and-scheduling.md: they exist so lighting v2 is an
#: addition rather than a migration, and are REJECTED at validation until the
#: solar maths lands. Accepting them silently and behaving like ``clock`` would
#: be worse than refusing — a reefkeeper would get a plausible-looking schedule
#: that was not the one they asked for.
Anchor = Literal["clock", "solar_natural", "solar_custom"]

_V2_ANCHORS = frozenset({"solar_natural", "solar_custom"})

#: What to do about an event that should have happened while we were down.
#:
#: ``converge`` — a ramp is *state*, not a series of events: on wake, compute
#: what the level should be now and go there. ``skip`` — a discrete action never
#: fires late; it is skipped and audited, the scheduling twin of command expiry.
#: Dosing-shaped things are always ``skip``, because a late dose is exactly what
#: the expiry machinery exists to prevent.
OnMiss = Literal["converge", "skip"]


class Locale(BaseModel):
    """A reef whose day shape the profile is modelled on.

    Schema-now, v2-build: carried and validated in v1, consumed by the solar
    maths in lighting v2.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


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
    anchor: Anchor
    zone: str = "UTC"
    points: tuple[RampPoint, ...] = Field(min_length=2)
    locale: Locale | None = None
    on_miss: OnMiss = "converge"

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.anchor in _V2_ANCHORS:
            raise ValueError(
                f"anchor={self.anchor!r} needs the solar model, which ships with "
                "lighting v2. Use anchor='clock' for a point-based profile. The "
                "field exists now so v2 is an addition, not a migration."
            )
        if self.anchor == "clock" and self.locale is not None:
            # A locale on a clock profile does nothing, and a setting that
            # silently does nothing is a bug report waiting to happen.
            raise ValueError(
                "locale is only meaningful with a solar anchor; a clock profile ignores it"
            )

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

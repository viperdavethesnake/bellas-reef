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

This model **is a wire contract now**: :class:`ChannelProfile` is the
engine's read side of the curve the app writes as
:class:`~bellasreef_contracts.schedules.ScheduleDefinition`, per spec
2026-08-19. ``RampPoint`` is an alias for
:class:`~bellasreef_contracts.schedules.SchedulePoint` and curve validation
(:func:`~bellasreef_contracts.schedules.validate_curve`) is shared with the
API rather than duplicated — one rule for what a valid curve is, so an API
write and an engine read can never disagree about it.

**The engine never clamps duty.** Sub-8% output is undefined on the XLG
drivers, and the ruling is snap-to-0 — but that floor belongs to the PCA9685
driver, which knows the hardware. An engine that pre-clamped would make the
driver's floor untestable and would lie about what the schedule asked for.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from typing import Self
from zoneinfo import ZoneInfo

from bellasreef_contracts.schedules import (
    CHANNEL_ID_PATTERN,
    Anchor,
    Locale,
    OnMiss,
    ScheduleDefinition,
    SchedulePoint,
    validate_curve,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["Anchor", "ChannelProfile", "Locale", "OnMiss", "RampPoint"]

#: The engine's point IS the wire point (spec §5) — one source of truth for
#: what a curve point is, not a lookalike copy that could drift from it.
RampPoint = SchedulePoint


class ChannelProfile(BaseModel):
    """A named PWM channel and its day.

    At least two points are required. One point is not a profile — it is a
    constant, and expressing it as a schedule hides that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel_id: str = Field(pattern=CHANNEL_ID_PATTERN)
    anchor: Anchor
    zone: str = "UTC"
    points: tuple[RampPoint, ...] = Field(min_length=2)
    locale: Locale | None = None
    on_miss: OnMiss = "converge"

    @model_validator(mode="after")
    def _validate(self) -> Self:
        # One curve-validity rule, shared with the API's ScheduleDefinition —
        # see bellasreef_contracts.schedules for what it checks and why.
        validate_curve(self.points, self.zone, self.anchor, self.locale)
        return self

    @classmethod
    def from_definition(cls, channel_id: str, definition: ScheduleDefinition) -> ChannelProfile:
        """An assignment row made concrete: this channel plays this schedule."""
        return cls(
            channel_id=channel_id,
            anchor=definition.anchor,
            zone=definition.zone,
            points=definition.points,
            locale=definition.locale,
        )

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

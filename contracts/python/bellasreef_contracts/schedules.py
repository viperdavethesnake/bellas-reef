# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The shared lighting-curve model (PRD R7, docs/contracts/time-and-scheduling.md).

A curve is a set of (time-of-day, duty) points, linearly interpolated,
wrapping across midnight. Sunrise, sunset and a midday peak are just points;
there is no special-cased "sunrise" concept, because a reefkeeper who wants
two peaks or a lunar channel should not need new code.

**Timezone is explicit and per-schedule, and that is the DST decision made
expressible rather than made for you.** Points are wall-clock in the named
zone:

* ``zone="UTC"`` — fixed offset. The photoperiod is stable year-round and
  sunrise drifts against civil time by an hour twice a year.
* ``zone="America/Los_Angeles"`` — civil time. Sunrise stays at 08:00 on the
  clock, and the photoperiod jumps an hour twice a year.

Corals track light, not clocks, so the first is arguably better husbandry and
the second is what people expect from a schedule. This module supports both
and tests both, so the decision stays a config value rather than a rewrite.

**This lives in contracts, not in the engine, because API writes and engine
reads must validate against exactly one definition of "valid curve".** Two
copies of this validation is two things that can drift — an API that accepts
a curve the engine would reject is a schedule that silently never runs.
``validate_curve`` is the one rule; :class:`ScheduleDefinition` is the API's
wire shape and the engine's ``ChannelProfile`` (services/control_engine)
calls the same function from its own ``points``/``zone``/``anchor``/``locale``
fields until a later task rewires it onto this model directly.

**The engine never clamps duty.** Sub-8% output is undefined on the XLG
drivers, and the ruling is snap-to-0 — but that floor belongs to the PCA9685
driver, which knows the hardware. Validating here would make the driver's
floor untestable and would lie about what the schedule asked for.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "Anchor",
    "Locale",
    "OnMiss",
    "ScheduleDefinition",
    "SchedulePoint",
    "validate_curve",
]

#: Where a schedule's day *shape* sits in the operator's day.
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


class _Frozen(BaseModel):
    """Base config shared by every contract model. Same idiom as messages.py."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Locale(_Frozen):
    """A reef whose day shape the schedule is modelled on.

    Schema-now, v2-build: carried and validated in v1, consumed by the solar
    maths in lighting v2.
    """

    name: str = Field(min_length=1, max_length=64)
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class SchedulePoint(_Frozen):
    """One anchor in a curve: at this local time, this duty."""

    at: time
    duty: float = Field(ge=0.0, le=1.0)

    @field_validator("at")
    @classmethod
    def _no_microseconds(cls, value: time) -> time:
        # Sub-second precision in a lighting schedule is noise that makes
        # curves compare unequal for no reason anyone can perceive.
        return value.replace(microsecond=0)

    @property
    def seconds(self) -> int:
        return self.at.hour * 3600 + self.at.minute * 60 + self.at.second


def validate_curve(
    points: Sequence[SchedulePoint], zone: str, anchor: Anchor, locale: Locale | None
) -> None:
    """The one curve-validity rule, shared by ScheduleDefinition (API writes)
    and the engine's ChannelProfile (reads). Raises ValueError."""
    if anchor in _V2_ANCHORS:
        raise ValueError(
            f"anchor={anchor!r} needs the solar model, which ships with lighting v2. "
            "Use anchor='clock'. The field exists now so v2 is an addition, not a migration."
        )
    if anchor == "clock" and locale is not None:
        raise ValueError(
            "locale is only meaningful with a solar anchor; a clock profile ignores it"
        )
    try:
        ZoneInfo(zone)
    except Exception as exc:
        raise ValueError(f"unknown timezone {zone!r}: {exc}") from exc
    seconds = [p.seconds for p in points]
    if seconds != sorted(seconds):
        raise ValueError("points must be in ascending time order")
    if len(set(seconds)) != len(seconds):
        raise ValueError("two points share the same time of day")


class ScheduleDefinition(_Frozen):
    """A named lighting curve: the API's wire shape for a schedule.

    At least two points are required. One point is not a curve — it is a
    constant, and expressing it as a schedule hides that.
    """

    name: str = Field(min_length=1, max_length=64)
    zone: str = "UTC"
    anchor: Anchor = "clock"
    locale: Locale | None = None
    points: tuple[SchedulePoint, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        validate_curve(self.points, self.zone, self.anchor, self.locale)
        return self

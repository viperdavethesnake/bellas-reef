# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Lighting scheduler (PRD R7).

Decides *what* should be commanded; publishing is somebody else's job. Keeping
the decision pure — clock in, intents out, no I/O — is what makes the profile
maths testable against fixed clocks instead of against wall time.

It is also what makes shadow mode (R3) cheap to add next pass: the scheduler
already produces intents rather than side effects, so shadow mode is a decision
about whether to hand them to the publisher, not a change to this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bellasreef_control_engine.profiles import ChannelProfile

__all__ = ["Intent", "LightingScheduler"]

#: A ramp from 0 to 100% over an hour moves ~0.03%/second. Emitting every tick
#: would be thousands of near-identical commands a day for no visible
#: difference, so a change has to be worth a message.
DEFAULT_DEADBAND = 0.005

#: Even when nothing changes, restate the level periodically. hardware-io holds
#: last-known state, but a service that restarted mid-photoperiod should not
#: have to wait for the next ramp segment to learn what a channel should be.
DEFAULT_REFRESH_S = 300.0


@dataclass(frozen=True, slots=True)
class Intent:
    """A decision, not yet a command."""

    channel_id: str
    duty: float
    reason: str


class LightingScheduler:
    """Turns profiles plus a clock into intents."""

    def __init__(
        self,
        profiles: list[ChannelProfile],
        *,
        deadband: float = DEFAULT_DEADBAND,
        refresh_s: float = DEFAULT_REFRESH_S,
    ) -> None:
        if deadband < 0:
            raise ValueError("deadband must be >= 0")
        if refresh_s <= 0:
            raise ValueError("refresh_s must be > 0")

        ids = [p.channel_id for p in profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate channel_id in profiles")

        self._profiles = profiles
        self._deadband = deadband
        self._refresh_s = refresh_s
        self._last_duty: dict[str, float] = {}
        self._last_emitted_at: dict[str, datetime] = {}

    @property
    def channel_ids(self) -> tuple[str, ...]:
        return tuple(p.channel_id for p in self._profiles)

    def due(self, now: datetime) -> list[Intent]:
        """Intents that should be published at ``now``.

        Pure: same clock, same state, same answer. Calling it does not mutate
        anything — :meth:`mark_emitted` does, and only for intents that were
        actually published. A scheduler that recorded an emission the publisher
        never made would silently skip the next one.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        intents: list[Intent] = []
        for profile in self._profiles:
            duty = profile.duty_at(now)
            previous = self._last_duty.get(profile.channel_id)

            if previous is None:
                intents.append(Intent(profile.channel_id, duty, "initial"))
                continue

            if abs(duty - previous) >= self._deadband:
                intents.append(Intent(profile.channel_id, duty, "ramp"))
                continue

            last_at = self._last_emitted_at.get(profile.channel_id)
            if last_at is None or (now - last_at).total_seconds() >= self._refresh_s:
                intents.append(Intent(profile.channel_id, duty, "refresh"))

        return intents

    def mark_emitted(self, intent: Intent, at: datetime) -> None:
        """Record that an intent was actually published."""
        self._last_duty[intent.channel_id] = intent.duty
        self._last_emitted_at[intent.channel_id] = at

    def reset(self) -> None:
        """Forget emission history.

        Used after a clock-trust gap: what was emitted against a clock we no
        longer believe tells us nothing about what to emit now.
        """
        self._last_duty.clear()
        self._last_emitted_at.clear()

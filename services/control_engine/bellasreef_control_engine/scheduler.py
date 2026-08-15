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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from bellasreef_control_engine.profiles import ChannelProfile

__all__ = ["SAFE_DUTY", "Intent", "LightingScheduler"]

#: What a channel is assumed to be at when we have no emission history.
#:
#: Not a guess. hardware-io drives every actuator to its declared safe state
#: at startup, and heartbeat loss does the same — so after any restart of
#: either service the channel really is dark. Converging *from* dark is
#: therefore the truthful starting point, and it is also the safe one if that
#: assumption is ever wrong: slewing up from 0 cannot slam a tank.
SAFE_DUTY = 0.0

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
        max_duty_delta_per_s: float | None = None,
    ) -> None:
        if deadband < 0:
            raise ValueError("deadband must be >= 0")
        if refresh_s <= 0:
            raise ValueError("refresh_s must be > 0")
        if max_duty_delta_per_s is not None and max_duty_delta_per_s <= 0:
            raise ValueError("max_duty_delta_per_s must be > 0, or None to disable")

        ids = [p.channel_id for p in profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate channel_id in profiles")

        self._profiles = profiles
        self._deadband = deadband
        self._refresh_s = refresh_s
        self._slew = max_duty_delta_per_s
        self._last_duty: dict[str, float] = {}
        self._last_emitted_at: dict[str, datetime] = {}

    @property
    def channel_ids(self) -> tuple[str, ...]:
        return tuple(p.channel_id for p in self._profiles)

    def due(self, now: datetime, overrides: Mapping[str, float] | None = None) -> list[Intent]:
        """Intents that should be published at ``now``.

        Pure: same clock, same state, same answer. Calling it does not mutate
        anything — :meth:`mark_emitted` does, and only for intents that were
        actually published. A scheduler that recorded an emission the publisher
        never made would silently skip the next one.

        Duty is **slew-limited** when a rate is configured. One mechanism covers
        all three causes named in the contract — restart convergence, a config
        edit mid-ramp, and override release — because from the tank's point of
        view they are the same event: the target moved and the light must not
        pop to it.

        ``overrides`` maps channel to a held duty. Passing them in rather than
        reaching for them keeps this function pure and testable against fixed
        clocks, which is the property the whole module is arranged around.

        A held channel with no configured profile — every channel adopted
        through the app, spec 2026-08-15 — is not silently dropped: it is
        treated as a constant schedule of :data:`SAFE_DUTY` that the override
        outranks, exactly like a profiled channel's curve. Release is just
        another target change, so the same slew/deadband/convergence/refresh
        machinery applies unchanged via :meth:`_emit_for`. Which unprofiled
        channels are even worth considering is derived from ``overrides``
        (currently held) and ``self._last_duty`` (still converging toward
        :data:`SAFE_DUTY` from a hold that ended) rather than from any state
        this method writes — that keeps ``due()`` itself free of mutation, so
        a channel that has fully converged and gone quiet never resurfaces on
        its own, and a repeated call with identical arguments still answers
        identically.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        held = overrides or {}
        intents: list[Intent] = []
        for profile in self._profiles:
            # An override outranks the schedule while it is owed. Slew still
            # applies on the way in and on release — from the tank's point of
            # view an override ending is just another target change.
            target = held.get(profile.channel_id, profile.duty_at(now))
            intent = self._emit_for(profile.channel_id, target, now)
            if intent is not None:
                intents.append(intent)

        # Held channels with no profile: an operator hold on an adopted
        # channel the config never mentions (every channel adopted through
        # the app, spec 2026-08-15). Semantics: a constant schedule of
        # SAFE_DUTY that the override outranks — release is just another
        # target change, and the same slew/deadband/convergence machinery
        # applies. Worth considering = currently held, or still converging
        # from a hold that has already ended; once converged and released it
        # drops out on its own and stays quiet.
        profiled = {p.channel_id for p in self._profiles}
        synthetic = (
            set(held) | {cid for cid, duty in self._last_duty.items() if duty != SAFE_DUTY}
        ) - profiled
        for channel_id in sorted(synthetic):
            target = held.get(channel_id, SAFE_DUTY)
            intent = self._emit_for(channel_id, target, now)
            if intent is not None:
                intents.append(intent)

        return intents

    def _emit_for(self, channel_id: str, target: float, now: datetime) -> Intent | None:
        """The shared per-channel emission decision.

        Given a target duty (a profile's curve, or an unprofiled channel's
        synthetic constant-SAFE_DUTY schedule), decide whether — and with what
        reason — a channel is due. Cold-start, slew-limited convergence,
        deadband suppression and periodic refresh are all target-agnostic, so
        this is the one place that logic lives; both loops in :meth:`due` call
        it rather than duplicating the block.
        """
        previous = self._last_duty.get(channel_id)
        cold = previous is None
        if previous is None:
            previous = SAFE_DUTY

        last_at = self._last_emitted_at.get(channel_id, now)
        duty = self._limit(previous, target, (now - last_at).total_seconds())
        converging = duty != target

        if cold:
            return Intent(channel_id, duty, "initial")
        if converging:
            # Mid-convergence. Emit even if this step is smaller than the
            # deadband, or a slow slew would stall short of the target and
            # sit there — the deadband exists to suppress noise, not
            # progress.
            return Intent(channel_id, duty, "converge")
        if abs(duty - previous) >= self._deadband:
            return Intent(channel_id, duty, "ramp")

        elapsed = (now - last_at).total_seconds()
        if channel_id not in self._last_emitted_at or elapsed >= self._refresh_s:
            return Intent(channel_id, duty, "refresh")
        return None

    def _limit(self, previous: float, target: float, dt_s: float) -> float:
        """Clamp a move toward ``target`` to the configured rate."""
        if self._slew is None:
            return target
        allowed = self._slew * max(dt_s, 0.0)
        if abs(target - previous) <= allowed:
            return target
        return previous + allowed if target > previous else previous - allowed

    def mark_emitted(self, intent: Intent, at: datetime) -> None:
        """Record that an intent was actually published."""
        self._last_duty[intent.channel_id] = intent.duty
        self._last_emitted_at[intent.channel_id] = at

    def reset(self) -> None:
        """Forget emission history for every channel.

        Used after a clock-trust gap: what was emitted against a clock we no
        longer believe tells us nothing about what to emit now.
        """
        self._last_duty.clear()
        self._last_emitted_at.clear()

    def forget(self, channel_id: str) -> None:
        """Forget emission history for one channel.

        Driven by the tombstone EVENT (``AssignmentLedger.on_tombstone``,
        wired in ``ControlEngine.__init__``), not by a tick. A tick-timed
        forget can only ever act on a channel ``due()`` actually surfaces —
        which happens only when it is cold, mid-slew, past the deadband, or
        past the refresh window — so a tombstone landing outside all four
        (adopt, publish, unadopt shortly after, re-adopt shortly after, all
        well inside the deadband/refresh windows) would never trigger it.
        Firing straight off the tombstone has no such gap, and firing on
        every tombstone rather than only ones for previously-adopted channels
        is deliberate: forgetting an already-cold channel is a no-op, and
        that is simpler and more robust than re-deriving "was this adopted"
        here.

        hardware-io tears the driver down and rebuilds it dark on the next
        adoption, so the duty this scheduler last emitted stops being true
        the instant the tombstone lands — it describes hardware that no
        longer exists. Left unforgotten, the *next* adopt would see a
        non-``None`` ``_last_duty`` for this channel and treat the following
        tick as a continuation ("ramp" / "converge") of the old run instead
        of a fresh cold start, jumping the newly-rebuilt-dark channel
        straight to whatever duty was last emitted
        before the tombstone — with no slew, because nothing marks this as a
        cold start once ``_last_duty`` is populated. Clearing it here forces
        the first post-readoption intent back to "initial", converging from
        :data:`SAFE_DUTY` like any other cold start.
        """
        self._last_duty.pop(channel_id, None)
        self._last_emitted_at.pop(channel_id, None)

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

from bellasreef_db import Transition

from bellasreef_control_engine.profiles import ChannelProfile

__all__ = ["SAFE_DUTY", "HeldTarget", "Intent", "LightingScheduler"]

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
    """A decision, not yet a command.

    ``hold`` is the transition of the hold this intent was emitted under, or
    None when the channel was not held. :meth:`LightingScheduler.mark_emitted`
    reads it to keep the scheduler's per-channel hold memory exact without
    :meth:`LightingScheduler.due` mutating anything.
    """

    channel_id: str
    duty: float
    reason: str
    hold: Transition | None = None
    #: True when this intent was slew-limited short of its target — the
    #: channel is still on its way. :meth:`LightingScheduler.mark_emitted`
    #: remembers it so the *arrival* step (the one that finally equals the
    #: target) is emitted even when it is smaller than the deadband. Without
    #: this a ramp to 100 % published 0.9956 and then nothing until the
    #: 300 s refresh (bench 2026-08-17: 3.294 V, "Now 99 %").
    converging: bool = False


@dataclass(frozen=True, slots=True)
class HeldTarget:
    """An operator hold as the scheduler sees it: a duty, and how to get there.

    ``transition`` is the operator's choice per hold (spec 2026-08-17). "snap"
    moves in one step regardless of the configured slew; "ramp" is today's
    slew-limited path. It governs both ends — arrival and release/expiry.
    """

    duty: float
    transition: Transition


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
        #: Transition of the last hold each channel was emitted under. Written
        #: and cleared only by mark_emitted (from Intent.hold), so due() stays
        #: pure. Consulted on the first tick a channel is no longer held: a
        #: snap hold releases in one step; anything else slews as before.
        #:
        #: Asymmetric by design: a stale "ramp" entry can persist when a ramp
        #: hold ends with the channel already sitting at its resting target —
        #: release emits no intent in that case, so mark_emitted never runs
        #: and the entry is never cleared. Harmless: "ramp" is the fallback
        #: behaviour (both "no entry" and "entry says ramp" take the same
        #: path in _emit_for), so the only effect of the stale entry is that
        #: a later ramp hold at the same duty is not re-announced with reason
        #: "hold" — it was already the behaviour with no entry at all. A
        #: "snap" entry never goes stale the same way, because due() keeps a
        #: channel with a remembered snap hold in the synthetic set even once
        #: it is unprofiled and no longer held, so the one-step release is
        #: always surfaced — and a snap release always emits one command,
        #: even when the channel already sits at the resting duty, precisely
        #: because that emission is what clears the memory.
        self._last_hold: dict[str, Transition] = {}
        #: Channels whose last emitted intent was slew-limited short of its
        #: target (Intent.converging). Written only by mark_emitted, so due()
        #: stays pure. The arrival step bypasses the deadband for these.
        self._slewing: set[str] = set()

    @property
    def channel_ids(self) -> tuple[str, ...]:
        return tuple(p.channel_id for p in self._profiles)

    def set_profiles(self, profiles: list[ChannelProfile]) -> None:
        """Swap the schedule set in place. Emission history is deliberately kept:
        a changed curve is a moved target the slew converges to; a removed
        assignment falls into the synthetic-channel path and converges to
        SAFE_DUTY; a held channel keeps its hold memory. Clearing history here
        would make every schedule edit a cold start — a visible pop for no reason.
        """
        ids = [p.channel_id for p in profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate channel_id in profiles")
        self._profiles = profiles

    def due(self, now: datetime, overrides: Mapping[str, HeldTarget] | None = None) -> list[Intent]:
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

        ``overrides`` maps channel to a :class:`HeldTarget` — the held duty and
        how to move to it. Passing them in rather than reaching for them keeps
        this function pure and testable against fixed clocks, which is the
        property the whole module is arranged around.

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
            # An override outranks the schedule while it is owed. How the
            # channel moves — snap or slew — is the hold's decision, on the
            # way in and on release alike.
            intent = self._emit_for(
                profile.channel_id, profile.duty_at(now), now, hold=held.get(profile.channel_id)
            )
            if intent is not None:
                intents.append(intent)

        # Held channels with no profile: an operator hold on an adopted
        # channel the config never mentions (every channel adopted through
        # the app, spec 2026-08-15). Semantics: a constant schedule of
        # SAFE_DUTY that the override outranks — release is just another
        # target change. Worth considering = currently held, still converging
        # from a hold that has already ended, or remembered as a snap hold
        # whose one-step release has not been emitted yet; once released and
        # converged it drops out on its own and stays quiet.
        profiled = {p.channel_id for p in self._profiles}
        synthetic = (
            set(held)
            | {cid for cid, duty in self._last_duty.items() if duty != SAFE_DUTY}
            | {cid for cid, t in self._last_hold.items() if t == "snap"}
        ) - profiled
        for channel_id in sorted(synthetic):
            intent = self._emit_for(channel_id, SAFE_DUTY, now, hold=held.get(channel_id))
            if intent is not None:
                intents.append(intent)

        return intents

    def _emit_for(
        self, channel_id: str, resting: float, now: datetime, *, hold: HeldTarget | None
    ) -> Intent | None:
        """The shared per-channel emission decision.

        ``resting`` is what the channel should be at when nobody holds it (a
        profile's curve, or SAFE_DUTY for an unprofiled channel); ``hold`` is
        the operator's override if one is owed. Cold-start, convergence,
        deadband suppression and periodic refresh are target-agnostic, so
        this is the one place that logic lives; both loops in :meth:`due` call
        it rather than duplicating the block.

        Transition rule (spec 2026-08-17): a target that comes from a hold
        moves the way that hold says. A snap hold's intent *is* the target;
        a ramp hold goes through :meth:`_limit` like a schedule. A hold's
        arrival, or a change of transition while held, is always announced
        (reason ``hold``) even inside the deadband, so that after
        :meth:`mark_emitted` the hold memory is exact. On the first tick a
        channel is no longer held, a remembered snap hold releases to
        ``resting`` in one step (reason ``release``); anything else slews.
        """
        previous = self._last_duty.get(channel_id)
        cold = previous is None
        if previous is None:
            previous = SAFE_DUTY
        last_at = self._last_emitted_at.get(channel_id, now)
        dt_s = (now - last_at).total_seconds()
        remembered = self._last_hold.get(channel_id)

        if hold is None:
            if remembered == "snap":
                return Intent(channel_id, resting, "release")
            target = resting
            duty = self._limit(previous, target, dt_s)
            tag: Transition | None = None
        else:
            target = hold.duty
            duty = target if hold.transition == "snap" else self._limit(previous, target, dt_s)
            tag = hold.transition
        converging = duty != target

        if hold is not None:
            if remembered != hold.transition:
                # Arrival, or a supersede that changed the mode: announce it,
                # deadband or not, so mark_emitted records the new memory.
                return Intent(channel_id, duty, "hold", tag, converging)
            if hold.transition == "snap" and duty != previous:
                # A snap hold re-held at a new duty: still a jump, still a hold.
                return Intent(channel_id, duty, "hold", tag, converging)

        if cold:
            return Intent(channel_id, duty, "initial", tag, converging)
        if converging:
            # Mid-convergence. Emit even if this step is smaller than the
            # deadband, or a slow slew would stall short of the target and
            # sit there — the deadband exists to suppress noise, not
            # progress.
            return Intent(channel_id, duty, "converge", tag, converging=True)
        if abs(duty - previous) >= self._deadband:
            return Intent(channel_id, duty, "ramp", tag)
        if channel_id in self._slewing:
            # The arrival step that ends a convergence, when the residual is
            # smaller than the deadband: `_limit` returned the target,
            # `converging` went false, and without this clause the deadband
            # swallowed the last step — a ramp to 100 % published 0.9956 and
            # then nothing until refresh (bench 2026-08-17: 3.294 V on LED0,
            # "Now 99 %"). Arrival is progress, not noise.
            return Intent(channel_id, duty, "converge", tag)

        if channel_id not in self._last_emitted_at or dt_s >= self._refresh_s:
            return Intent(channel_id, duty, "refresh", tag)
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
        """Record that an intent was actually published.

        Also the only writer of the hold memory: an intent emitted under a
        hold records that hold's transition; an intent emitted while not held
        (a release, or ordinary convergence back to the resting target)
        clears it. A hold whose intent never went out is never remembered.
        """
        self._last_duty[intent.channel_id] = intent.duty
        self._last_emitted_at[intent.channel_id] = at
        if intent.hold is None:
            self._last_hold.pop(intent.channel_id, None)
        else:
            self._last_hold[intent.channel_id] = intent.hold
        if intent.converging:
            self._slewing.add(intent.channel_id)
        else:
            self._slewing.discard(intent.channel_id)

    def reset(self) -> None:
        """Forget emission history for every channel.

        Used after a clock-trust gap: what was emitted against a clock we no
        longer believe tells us nothing about what to emit now.
        """
        self._last_duty.clear()
        self._last_emitted_at.clear()
        self._last_hold.clear()
        self._slewing.clear()

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

        The hold memory goes with it — a re-adopted channel must not
        snap-release on the strength of a hold that ended before its driver
        was rebuilt.
        """
        self._last_duty.pop(channel_id, None)
        self._last_emitted_at.pop(channel_id, None)
        self._last_hold.pop(channel_id, None)
        self._slewing.discard(channel_id)

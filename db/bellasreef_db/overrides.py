# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Manual overrides (docs/contracts/time-and-scheduling.md §4).

Lives in the schema package, not the engine: both the control engine (which
honours overrides) and the API (which creates and releases them) need it, and
the API must not import the control engine — a stateless front door has no
business depending on the control loops.

An override is a *duration*, not a schedule: "off for 30 minutes" means 1800
elapsed seconds, which makes it immune to DST and timezone by construction.

Two clocks, deliberately:

* **Within a run** the deadline is monotonic. Wall time can jump — chrony steps
  it on this RTC-less board — and an override that shortened or lengthened
  because NTP corrected the clock would be a surprise on a tank.
* **Across a restart** only a wall-clock deadline survives, since the monotonic
  origin dies with the process. So ``expires_at`` is persisted purely to decide,
  on wake, whether the override is still owed.

**Lapse-on-wake:** if the engine was down past an override's expiry, the
override lapses and the schedule resumes. An override that outlives its promise
is a silent trap — the operator asked for thirty minutes, and coming back to a
dark tank hours later because the engine restarted is not that.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID, uuid4

from bellasreef_service import clock_is_trusted
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "TRANSITIONS",
    "ActiveOverride",
    "ClockUntrustedError",
    "OverrideStore",
    "PlacedOverride",
    "ReleaseReason",
    "ReleasedOverride",
    "Transition",
    "WakeReport",
]


class ClockUntrustedError(RuntimeError):
    """Raised when an override is placed against a clock we cannot believe."""


ReleaseReason = Literal["expired", "lapsed", "manual", "superseded"]

#: How the engine moves the light to a held level and back. "snap" is one
#: step; "ramp" is the global slew. Governs both ends of the hold (spec
#: 2026-08-17). Kept beside ReleaseReason because the API and the engine
#: both import it from here — one source of truth per vocabulary.
Transition = Literal["snap", "ramp"]
TRANSITIONS: Final[frozenset[str]] = frozenset({"snap", "ramp"})


def _transition(raw: object) -> Transition:
    value = str(raw)
    if value == "snap":
        return "snap"
    if value == "ramp":
        return "ramp"
    # The CHECK constraint makes this unreachable; failing loudly beats
    # silently ramping a hold the operator asked to snap.
    raise ValueError(f"overrides.transition holds {value!r}, outside {sorted(TRANSITIONS)}")


@dataclass(slots=True)
class ActiveOverride:
    """An override the engine is currently honouring."""

    id: UUID
    target: str
    duty: float
    expires_at: datetime
    #: "snap" or "ramp" — see :data:`Transition`. Defaults to "ramp" only so
    #: dataclass construction sites predating the field keep working; every
    #: store read sets it explicitly from the row.
    transition: Transition = "ramp"
    #: Monotonic deadline, set when this process armed it. None for an override
    #: loaded from the database but not yet re-armed.
    monotonic_deadline: float | None = None

    def is_expired(
        self, *, monotonic_now: float | None = None, wall_now: datetime | None = None
    ) -> bool:
        """Prefer the monotonic deadline; fall back to wall clock.

        The fallback only applies before the override has been armed in this
        process — that is exactly the restart path, where wall clock is all
        there is.
        """
        if self.monotonic_deadline is not None:
            return (monotonic_now or _time.monotonic()) >= self.monotonic_deadline
        return (wall_now or datetime.now(UTC)) >= self.expires_at

    def arm(self, *, monotonic_now: float | None = None, wall_now: datetime | None = None) -> None:
        """Convert the remaining wall-clock time into a monotonic deadline."""
        remaining = (self.expires_at - (wall_now or datetime.now(UTC))).total_seconds()
        self.monotonic_deadline = (monotonic_now or _time.monotonic()) + max(remaining, 0.0)


@dataclass(frozen=True, slots=True)
class ReleasedOverride:
    """An override the store closed on the caller's behalf, named so the
    caller can audit it. The store never audits — it has no spine — so it
    reports what it released and leaves saying so to whoever asked."""

    id: UUID
    target: str


@dataclass(frozen=True, slots=True)
class PlacedOverride:
    """What :meth:`OverrideStore.create` did: the hold now live, and any hold
    it displaced on the same target.

    ``superseded`` is a tuple, not an optional, because the partial unique
    index promises at most one — but a caller that audits "superseded" per
    entry needs no special case for zero, and a caller that ever sees two has
    learned the index is gone. Captured with ``RETURNING`` in the same
    transaction as the insert, so it can never name a hold some other request
    superseded a millisecond earlier.
    """

    override: ActiveOverride
    superseded: tuple[ReleasedOverride, ...] = ()


@dataclass(frozen=True, slots=True)
class WakeReport:
    """What :meth:`OverrideStore.load_active` found at wake: the holds still
    owed, and the ones it lapsed because their deadline passed while the
    engine was down. The lapsed ones are endings nobody else witnessed —
    reported so the engine can put them in the audit trail beside the manual
    and expired releases the API and the tick loop record."""

    live: list[ActiveOverride]
    lapsed: tuple[ReleasedOverride, ...] = ()


class OverrideStore:
    """Persistence for overrides. One active per target, enforced by the index.

    Creation is **clock-gated**, with the same predicate that gates command
    emission. An override's whole meaning is a deadline, and a deadline
    computed from a clock that is about to be stepped by chrony is not the
    duration the operator asked for: place "off for 30 minutes" during the
    minute after a power cut and a correction of two hours turns it into an
    override that already expired, or one that outlives the evening.

    Refusing is the honest answer. The window is seconds-to-minutes on a
    healthy host, and an operator who is told "the clock is still syncing" can
    press the button again.
    """

    def __init__(
        self, engine: AsyncEngine, *, clock_trusted: Callable[[], bool] | None = None
    ) -> None:
        self._engine = engine
        # Held as an override rather than resolved now: looking the default
        # up at call time means the predicate can be swapped for a drill or a
        # test without rebuilding every store that already exists.
        self._clock_trusted_override = clock_trusted

    async def create(
        self,
        target: str,
        duty: float,
        duration_s: float,
        *,
        reason: str | None = None,
        transition: Transition = "ramp",
        now: datetime | None = None,
    ) -> PlacedOverride:
        """Place an override, superseding any active one on the same target.

        Superseding rather than rejecting: the operator pressing "off for 30
        minutes" again clearly means the new thirty minutes, and making them
        cancel first would be ceremony.

        Returns the placed hold *and* whatever it displaced, because a
        supersede is an ending the audit trail must show — the API records
        ``override.created`` for the new one and needs the old one's id to
        record ``override.released`` beside it. Before this the trail showed
        holds starting and never ending (UX review 2026-08-17, E1).
        """
        if duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if not 0.0 <= duty <= 1.0:
            raise ValueError("duty must be within 0.0-1.0")
        if transition not in TRANSITIONS:
            raise ValueError(f"transition must be one of {sorted(TRANSITIONS)}, got {transition!r}")
        trusted = self._clock_trusted_override or clock_is_trusted
        if not trusted():
            raise ClockUntrustedError(
                "the host clock is not synchronised; an override placed now would "
                "carry a deadline that chrony is about to move. Try again once the "
                "clock has settled."
            )

        issued = now or datetime.now(UTC)
        expires_at = issued + timedelta(seconds=duration_s)
        override_id = uuid4()

        async with self._engine.begin() as conn:
            displaced = (
                await conn.execute(
                    text(
                        "UPDATE overrides SET released_at = :now, release_reason = 'superseded' "
                        "WHERE target = :target AND released_at IS NULL RETURNING id, target"
                    ),
                    {"now": issued, "target": target},
                )
            ).all()
            await conn.execute(
                text(
                    "INSERT INTO overrides (id, target, level, reason, created_at, expires_at, "
                    "transition) VALUES (:id, :target, CAST(:level AS JSONB), :reason, "
                    ":created, :expires, :transition)"
                ),
                {
                    "id": override_id,
                    "target": target,
                    "level": f'{{"kind": "pwm", "duty": {duty}}}',
                    "reason": reason,
                    "created": issued,
                    "expires": expires_at,
                    "transition": transition,
                },
            )

        active = ActiveOverride(
            id=override_id,
            target=target,
            duty=duty,
            expires_at=expires_at,
            transition=transition,
        )
        active.arm(wall_now=issued)
        return PlacedOverride(
            override=active,
            superseded=tuple(
                ReleasedOverride(id=UUID(str(row[0])), target=str(row[1])) for row in displaced
            ),
        )

    async def load_active(self, *, now: datetime | None = None) -> WakeReport:
        """Overrides still owed at wake, lapsing any that aged out while down.

        This is the lapse-on-wake rule. Anything whose deadline passed during
        the outage is closed here and never applied. What was lapsed is
        reported alongside the survivors so the caller can audit it — the
        store cannot, and an ending nobody records is how a log ends up
        showing holds that start and never finish.
        """
        wall_now = now or datetime.now(UTC)

        async with self._engine.begin() as conn:
            lapsed = (
                await conn.execute(
                    text(
                        "UPDATE overrides SET released_at = :now, release_reason = 'lapsed' "
                        "WHERE released_at IS NULL AND expires_at <= :now RETURNING id, target"
                    ),
                    {"now": wall_now},
                )
            ).all()
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, target, level, expires_at, transition FROM overrides "
                        "WHERE released_at IS NULL"
                    )
                )
            ).all()

        live: list[ActiveOverride] = []
        for row in rows:
            override = ActiveOverride(
                id=UUID(str(row[0])),
                target=str(row[1]),
                duty=float(row[2]["duty"]),
                expires_at=row[3],
                transition=_transition(row[4]),
            )
            override.arm(wall_now=wall_now)
            live.append(override)
        return WakeReport(
            live=live,
            lapsed=tuple(
                ReleasedOverride(id=UUID(str(row[0])), target=str(row[1])) for row in lapsed
            ),
        )

    async def release(
        self, override_id: UUID, reason: ReleaseReason, *, now: datetime | None = None
    ) -> str | None:
        """Releases the row and returns its ``target``, or ``None`` if nothing
        matched (unknown id, or already released).

        Returning the target — not just success/failure — is what lets a
        caller that only holds ``override_id`` (the manual-release API path)
        stamp its audit row with the device the hold was actually on, instead
        of leaving ``device_id`` NULL on exactly the event this branch closes
        that hole for elsewhere.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE overrides SET released_at = :now, release_reason = :reason "
                    "WHERE id = :id AND released_at IS NULL "
                    "RETURNING target"
                ),
                {"now": now or datetime.now(UTC), "reason": reason, "id": override_id},
            )
            row = result.first()
            return str(row[0]) if row is not None else None

    async def active_for(self, target: str) -> ActiveOverride | None:
        """The live override on one target, if any."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, target, level, expires_at, transition FROM overrides "
                        "WHERE target = :target AND released_at IS NULL"
                    ),
                    {"target": target},
                )
            ).first()
        if row is None:
            return None
        return ActiveOverride(
            id=UUID(str(row[0])),
            target=str(row[1]),
            duty=float(row[2]["duty"]),
            expires_at=row[3],
            transition=_transition(row[4]),
        )

    async def list_active(self) -> list[ActiveOverride]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, target, level, expires_at, transition FROM overrides "
                        "WHERE released_at IS NULL ORDER BY created_at"
                    )
                )
            ).all()
        return [
            ActiveOverride(
                id=UUID(str(r[0])),
                target=str(r[1]),
                duty=float(r[2]["duty"]),
                expires_at=r[3],
                transition=_transition(r[4]),
            )
            for r in rows
        ]

    async def active_count(self, target: str) -> int:
        async with self._engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM overrides "
                            "WHERE target = :target AND released_at IS NULL"
                        ),
                        {"target": target},
                    )
                ).scalar_one()
            )

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Manual overrides (docs/contracts/time-and-scheduling.md §4).

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["ActiveOverride", "OverrideStore", "ReleaseReason"]

ReleaseReason = Literal["expired", "lapsed", "manual", "superseded"]


@dataclass(slots=True)
class ActiveOverride:
    """An override the engine is currently honouring."""

    id: UUID
    target: str
    duty: float
    expires_at: datetime
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


class OverrideStore:
    """Persistence for overrides. One active per target, enforced by the index."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(
        self,
        target: str,
        duty: float,
        duration_s: float,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ActiveOverride:
        """Place an override, superseding any active one on the same target.

        Superseding rather than rejecting: the operator pressing "off for 30
        minutes" again clearly means the new thirty minutes, and making them
        cancel first would be ceremony.
        """
        if duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if not 0.0 <= duty <= 1.0:
            raise ValueError("duty must be within 0.0-1.0")

        issued = now or datetime.now(UTC)
        expires_at = issued + timedelta(seconds=duration_s)
        override_id = uuid4()

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE overrides SET released_at = :now, release_reason = 'superseded' "
                    "WHERE target = :target AND released_at IS NULL"
                ),
                {"now": issued, "target": target},
            )
            await conn.execute(
                text(
                    "INSERT INTO overrides (id, target, level, reason, created_at, expires_at) "
                    "VALUES (:id, :target, CAST(:level AS JSONB), :reason, :created, :expires)"
                ),
                {
                    "id": override_id,
                    "target": target,
                    "level": f'{{"kind": "pwm", "duty": {duty}}}',
                    "reason": reason,
                    "created": issued,
                    "expires": expires_at,
                },
            )

        active = ActiveOverride(id=override_id, target=target, duty=duty, expires_at=expires_at)
        active.arm(wall_now=issued)
        return active

    async def load_active(self, *, now: datetime | None = None) -> list[ActiveOverride]:
        """Overrides still owed at wake, lapsing any that aged out while down.

        This is the lapse-on-wake rule. Anything whose deadline passed during
        the outage is closed here and never applied.
        """
        wall_now = now or datetime.now(UTC)

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE overrides SET released_at = :now, release_reason = 'lapsed' "
                    "WHERE released_at IS NULL AND expires_at <= :now"
                ),
                {"now": wall_now},
            )
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, target, level, expires_at FROM overrides "
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
            )
            override.arm(wall_now=wall_now)
            live.append(override)
        return live

    async def release(
        self, override_id: UUID, reason: ReleaseReason, *, now: datetime | None = None
    ) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE overrides SET released_at = :now, release_reason = :reason "
                    "WHERE id = :id AND released_at IS NULL"
                ),
                {"now": now or datetime.now(UTC), "reason": reason, "id": override_id},
            )
            return bool(result.rowcount)

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

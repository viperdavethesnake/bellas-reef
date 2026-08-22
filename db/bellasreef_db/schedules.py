# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Named lighting curves and which channel each one drives.

Lives in the schema package, not the engine: both the API (which creates,
edits and deletes named schedules, and assigns them to channels) and the
control engine (whose scheduler reads ``assigned_curves()`` every tick to know
what each channel should be doing right now) need it, and the API must not
import the control engine — a stateless front door has no business depending
on the control loops. Same reasoning as ``overrides.py:5-7``.

``points`` is stored verbatim as JSONB — the exact wire shape of
``bellasreef_contracts.schedules.ScheduleDefinition`` — so a written schedule
and a read one are the same JSON with no lossy round trip
(docs/superpowers/specs/2026-08-19-lighting-schedules-design.md, §Data
model). Validation (≥2 points, ascending unique times, duty in [0, 1], a
locale only with a solar anchor) lives once, in contracts, and runs on the
way in (``ScheduleDefinition`` construction) and on the way out (this store
re-validates on every read) — never a second, drifting copy of the rule here.

Assigning a schedule to a channel **replaces** whatever was assigned before
(``INSERT ... ON CONFLICT (channel_id) DO UPDATE``) rather than erroring: the
operator picking a different curve for a channel clearly means the new curve,
and making them unassign first would be ceremony — the same call David made
for overrides superseding each other.

Deleting a schedule that is still assigned is refused. This store checks
assignments explicitly first, so the caller gets a clean
:class:`ScheduleInUseError` naming what is in the way; the foreign key
(``fk_schedule_assignments_schedule_id_lighting_schedules``, ``ON DELETE
RESTRICT``) is the backstop, not the primary path — the forgetDevice lesson
(deleting a referenced row must be a rejection, not a 500; d2b35e3), applied
here before it could recur.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from bellasreef_contracts.schedules import ScheduleDefinition
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["ScheduleInUseError", "ScheduleStore", "StoredSchedule"]

#: The named constraint from migration 0019, matched against the wrapped
#: driver exception's text. Named so the ValueError this store raises for a
#: duplicate name comes from catching the real constraint violation rather
#: than a racy check-then-insert.
_UNIQUE_NAME_CONSTRAINT = "uq_lighting_schedules_name"


@dataclass(frozen=True, slots=True)
class StoredSchedule:
    """A schedule as it lives in Postgres: an id, its validated definition,
    and the channels currently assigned to it."""

    id: UUID
    definition: ScheduleDefinition
    assigned_channels: tuple[str, ...]


class ScheduleInUseError(RuntimeError):
    """Raised when a delete is refused because a channel is still assigned."""


def _dump_definition(definition: ScheduleDefinition) -> dict[str, Any]:
    """The wire shape, JSON-mode: ``time`` becomes an ISO string, so the
    result is exactly what ``json.dumps`` (and Postgres JSONB) can hold."""
    return definition.model_dump(mode="json")


def _bind_params(schedule_id: UUID, dump: dict[str, Any]) -> dict[str, Any]:
    """The bind params shared by ``create`` and ``update``: points and locale
    as JSON text for ``CAST(... AS JSONB)``, everything else as-is."""
    locale = dump["locale"]
    return {
        "id": schedule_id,
        "name": dump["name"],
        "points": json.dumps(dump["points"]),
        "zone": dump["zone"],
        "anchor": dump["anchor"],
        "locale": json.dumps(locale) if locale is not None else None,
    }


def _build_definition(
    schedule_id: UUID,
    *,
    name: str,
    points: Any,
    zone: str,
    anchor: str,
    locale: Any,
) -> ScheduleDefinition:
    """Rebuild and re-validate a definition from stored columns.

    A row can only get here if it was written outside this store, or if a
    validation rule was tightened after it was written. Either way, a
    definition that no longer validates must fail loudly and name the
    schedule — never come back half-built or get silently skipped, which is
    exactly the trap the audit-log and skip-declaration rules elsewhere in
    this codebase exist to close.
    """
    try:
        return ScheduleDefinition.model_validate(
            {"name": name, "zone": zone, "anchor": anchor, "locale": locale, "points": points}
        )
    except ValidationError as exc:
        raise ValueError(
            f"stored schedule {schedule_id} ({name!r}) no longer validates: {exc}"
        ) from exc


class ScheduleStore:
    """Persistence for named lighting curves and their channel assignments."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, definition: ScheduleDefinition) -> StoredSchedule:
        """Insert a new named schedule. Raises ``ValueError`` if the name is
        already taken, caught from the unique-constraint violation rather
        than a pre-check that could race a concurrent create."""
        schedule_id = uuid4()
        dump = _dump_definition(definition)
        async with self._engine.begin() as conn:
            try:
                await conn.execute(
                    text(
                        "INSERT INTO lighting_schedules (id, name, points, zone, anchor, locale) "
                        "VALUES (:id, :name, CAST(:points AS JSONB), :zone, :anchor, "
                        "CAST(:locale AS JSONB))"
                    ),
                    _bind_params(schedule_id, dump),
                )
            except IntegrityError as exc:
                if _UNIQUE_NAME_CONSTRAINT in str(exc.orig):
                    raise ValueError(
                        f"a schedule named {definition.name!r} already exists"
                    ) from exc
                raise
        return StoredSchedule(id=schedule_id, definition=definition, assigned_channels=())

    async def update(self, schedule_id: UUID, definition: ScheduleDefinition) -> StoredSchedule:
        """Replace a schedule's definition in place. Raises ``KeyError`` if
        the id is unknown, ``ValueError`` on a name collision with another
        schedule."""
        dump = _dump_definition(definition)
        async with self._engine.begin() as conn:
            try:
                result = await conn.execute(
                    text(
                        "UPDATE lighting_schedules SET name = :name, "
                        "points = CAST(:points AS JSONB), zone = :zone, anchor = :anchor, "
                        "locale = CAST(:locale AS JSONB), updated_at = now() WHERE id = :id"
                    ),
                    _bind_params(schedule_id, dump),
                )
            except IntegrityError as exc:
                if _UNIQUE_NAME_CONSTRAINT in str(exc.orig):
                    raise ValueError(
                        f"a schedule named {definition.name!r} already exists"
                    ) from exc
                raise
            if result.rowcount == 0:
                raise KeyError(schedule_id)
            assigned = (
                await conn.execute(
                    text("SELECT channel_id FROM schedule_assignments WHERE schedule_id = :id"),
                    {"id": schedule_id},
                )
            ).all()
        return StoredSchedule(
            id=schedule_id,
            definition=definition,
            assigned_channels=tuple(sorted(str(r[0]) for r in assigned)),
        )

    async def delete(self, schedule_id: UUID) -> None:
        """Delete a schedule. Raises ``ScheduleInUseError`` if a channel is
        still assigned to it, ``KeyError`` if the id is unknown."""
        async with self._engine.begin() as conn:
            assigned = (
                await conn.execute(
                    text(
                        "SELECT channel_id FROM schedule_assignments "
                        "WHERE schedule_id = :id LIMIT 1"
                    ),
                    {"id": schedule_id},
                )
            ).first()
            if assigned is not None:
                raise ScheduleInUseError(
                    f"schedule {schedule_id} is assigned to channel {assigned[0]!r}; "
                    "unassign it before deleting"
                )
            result = await conn.execute(
                text("DELETE FROM lighting_schedules WHERE id = :id"), {"id": schedule_id}
            )
            if result.rowcount == 0:
                raise KeyError(schedule_id)

    async def get(self, schedule_id: UUID) -> StoredSchedule:
        """Raises ``KeyError`` if the id is unknown, ``ValueError`` if the
        stored row no longer validates as a curve."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT name, points, zone, anchor, locale FROM lighting_schedules "
                        "WHERE id = :id"
                    ),
                    {"id": schedule_id},
                )
            ).first()
            if row is None:
                raise KeyError(schedule_id)
            assigned = (
                await conn.execute(
                    text("SELECT channel_id FROM schedule_assignments WHERE schedule_id = :id"),
                    {"id": schedule_id},
                )
            ).all()
        definition = _build_definition(
            schedule_id, name=row[0], points=row[1], zone=row[2], anchor=row[3], locale=row[4]
        )
        return StoredSchedule(
            id=schedule_id,
            definition=definition,
            assigned_channels=tuple(sorted(str(r[0]) for r in assigned)),
        )

    async def list(self) -> list[StoredSchedule]:
        """Raises ``ValueError`` naming the offending schedule if any stored
        row no longer validates — a listing never silently drops one."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, name, points, zone, anchor, locale FROM lighting_schedules "
                        "ORDER BY name"
                    )
                )
            ).all()
            assigned_rows = (
                await conn.execute(text("SELECT channel_id, schedule_id FROM schedule_assignments"))
            ).all()
        assigned_by_schedule: dict[UUID, list[str]] = {}
        for channel_id, schedule_id in assigned_rows:
            assigned_by_schedule.setdefault(UUID(str(schedule_id)), []).append(str(channel_id))
        result: list[StoredSchedule] = []
        for row in rows:
            schedule_id = UUID(str(row[0]))
            definition = _build_definition(
                schedule_id, name=row[1], points=row[2], zone=row[3], anchor=row[4], locale=row[5]
            )
            result.append(
                StoredSchedule(
                    id=schedule_id,
                    definition=definition,
                    assigned_channels=tuple(sorted(assigned_by_schedule.get(schedule_id, []))),
                )
            )
        return result

    async def assign(self, channel_id: str, schedule_id: UUID) -> None:
        """Assign a schedule to a channel, replacing any existing assignment
        for that channel. Raises ``KeyError`` if the schedule is unknown."""
        async with self._engine.begin() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM lighting_schedules WHERE id = :id"), {"id": schedule_id}
                )
            ).first()
            if exists is None:
                raise KeyError(schedule_id)
            await conn.execute(
                text(
                    "INSERT INTO schedule_assignments (channel_id, schedule_id) "
                    "VALUES (:channel_id, :schedule_id) "
                    "ON CONFLICT (channel_id) DO UPDATE SET schedule_id = EXCLUDED.schedule_id"
                ),
                {"channel_id": channel_id, "schedule_id": schedule_id},
            )

    async def unassign(self, channel_id: str) -> bool:
        """Remove a channel's assignment, if any. Returns whether one existed."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM schedule_assignments WHERE channel_id = :channel_id"),
                {"channel_id": channel_id},
            )
            return bool(result.rowcount)

    async def assigned_curves(self) -> dict[str, ScheduleDefinition]:
        """Channel id -> definition, for every currently-assigned channel.

        **This is the engine read** — the scheduler's tick calls this once
        and gets exactly the map it needs, one join, rather than fetching
        every schedule and every assignment and joining them itself.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT sa.channel_id, ls.id, ls.name, ls.points, ls.zone, ls.anchor, "
                        "ls.locale FROM schedule_assignments sa "
                        "JOIN lighting_schedules ls ON ls.id = sa.schedule_id"
                    )
                )
            ).all()
        curves: dict[str, ScheduleDefinition] = {}
        for row in rows:
            channel_id = str(row[0])
            schedule_id = UUID(str(row[1]))
            curves[channel_id] = _build_definition(
                schedule_id, name=row[2], points=row[3], zone=row[4], anchor=row[5], locale=row[6]
            )
        return curves

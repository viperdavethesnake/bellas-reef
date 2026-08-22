# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""ScheduleStore: named curves, assign-replaces, delete-in-use refuses.

Needs a real Postgres — the duplicate-name rejection is a unique constraint
(``uq_lighting_schedules_name``), the assign-replaces behaviour is an
``ON CONFLICT`` upsert, and the delete-in-use refusal backstops on
``fk_schedule_assignments_schedule_id_lighting_schedules`` (``ON DELETE
RESTRICT``) — none of that is SQLAlchemy-core behaviour a mock could prove.
"""

from __future__ import annotations

import json
from datetime import time
from uuid import UUID, uuid4

import pytest
from bellasreef_contracts.schedules import ScheduleDefinition, SchedulePoint
from bellasreef_db.schedules import ScheduleInUseError, ScheduleStore
from helpers import engine, requires_postgres, run
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = requires_postgres


async def fresh() -> AsyncEngine:
    eng = engine()
    async with eng.begin() as conn:
        # Both tables in one TRUNCATE: schedule_assignments FKs to
        # lighting_schedules, and Postgres requires the referencing table be
        # named too (or CASCADE) — listing both here does that.
        await conn.execute(text("TRUNCATE schedule_assignments, lighting_schedules"))
    return eng


def _definition(name: str, **overrides: object) -> ScheduleDefinition:
    payload: dict[str, object] = {
        "name": name,
        "points": (
            SchedulePoint(at=time(8, 0), duty=0.0),
            SchedulePoint(at=time(20, 0), duty=1.0),
        ),
    }
    payload.update(overrides)
    return ScheduleDefinition(**payload)  # type: ignore[arg-type]


class TestCreateGetList:
    def test_create_get_list_round_trip(self) -> None:
        """Points survive the JSONB round trip intact: what ``get`` returns,
        rebuilt from stored JSON via ``ScheduleDefinition.model_validate``,
        equals what was written."""

        async def scenario() -> tuple[ScheduleDefinition, ScheduleDefinition, list[UUID]]:
            eng = await fresh()
            store = ScheduleStore(eng)
            definition = _definition("Reef Day")
            created = await store.create(definition)
            assert created.definition == definition
            assert created.assigned_channels == ()

            fetched = await store.get(created.id)
            listed = await store.list()
            await eng.dispose()
            return fetched.definition, definition, [s.id for s in listed if s.id == created.id]

        fetched, definition, matches = run(scenario)
        assert fetched == definition
        assert matches == [matches[0]]  # created row is present in list()

    def test_create_duplicate_name_raises_value_error(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            await store.create(_definition("Sunrise Sunset"))
            with pytest.raises(ValueError, match="Sunrise Sunset"):
                await store.create(_definition("Sunrise Sunset"))
            await eng.dispose()

        run(scenario)

    def test_get_unknown_raises_key_error(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            with pytest.raises(KeyError):
                await store.get(uuid4())
            await eng.dispose()

        run(scenario)

    def test_list_returns_all_created_schedules(self) -> None:
        async def scenario() -> tuple[set[UUID], set[UUID]]:
            eng = await fresh()
            store = ScheduleStore(eng)
            created_a = await store.create(_definition("Alpha"))
            created_b = await store.create(_definition("Beta"))
            listed = await store.list()
            await eng.dispose()
            return {s.id for s in listed}, {created_a.id, created_b.id}

        listed_ids, created_ids = run(scenario)
        assert listed_ids == created_ids


class TestUpdate:
    def test_update_replaces_points(self) -> None:
        async def scenario() -> tuple[ScheduleDefinition, ScheduleDefinition, ScheduleDefinition]:
            eng = await fresh()
            store = ScheduleStore(eng)
            created = await store.create(_definition("Ramp"))
            new_definition = _definition(
                "Ramp",
                points=(
                    SchedulePoint(at=time(7, 0), duty=0.1),
                    SchedulePoint(at=time(12, 0), duty=1.0),
                    SchedulePoint(at=time(21, 0), duty=0.0),
                ),
            )
            updated = await store.update(created.id, new_definition)
            fetched = await store.get(created.id)
            await eng.dispose()
            return updated.definition, fetched.definition, new_definition

        updated_definition, fetched_definition, expected = run(scenario)
        assert updated_definition == expected
        assert fetched_definition == expected

    def test_update_unknown_raises_key_error(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            with pytest.raises(KeyError):
                await store.update(uuid4(), _definition("Nope"))
            await eng.dispose()

        run(scenario)

    def test_update_to_duplicate_name_raises_value_error(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            await store.create(_definition("Existing"))
            other = await store.create(_definition("Other"))
            with pytest.raises(ValueError, match="Existing"):
                await store.update(other.id, _definition("Existing"))
            await eng.dispose()

        run(scenario)


class TestDelete:
    def test_delete_unknown_raises_key_error(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            with pytest.raises(KeyError):
                await store.delete(uuid4())
            await eng.dispose()

        run(scenario)

    def test_delete_assigned_raises(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            s = await store.create(_definition("Bobs French Fries"))
            await store.assign("pi-pwm-0", s.id)
            with pytest.raises(ScheduleInUseError):
                await store.delete(s.id)
            await eng.dispose()

        run(scenario)

    def test_delete_unassigned_succeeds(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            s = await store.create(_definition("Gone Soon"))
            await store.delete(s.id)
            with pytest.raises(KeyError):
                await store.get(s.id)
            await eng.dispose()

        run(scenario)


class TestAssignments:
    def test_assign_unknown_schedule_raises_key_error(self) -> None:
        async def scenario() -> None:
            eng = await fresh()
            store = ScheduleStore(eng)
            with pytest.raises(KeyError):
                await store.assign("pi-pwm-0", uuid4())
            await eng.dispose()

        run(scenario)

    def test_assign_replaces_and_assigned_curves(self) -> None:
        async def scenario() -> tuple[
            dict[str, ScheduleDefinition],
            ScheduleDefinition,
            tuple[str, ...],
            tuple[str, ...],
        ]:
            eng = await fresh()
            store = ScheduleStore(eng)
            a = await store.create(_definition("This One"))
            b = await store.create(_definition("That One"))
            await store.assign("pi-pwm-0", a.id)
            await store.assign("pi-pwm-0", b.id)  # replaces, per David's ruling — no 409 here
            curves = await store.assigned_curves()
            a_assigned = (await store.get(a.id)).assigned_channels
            b_assigned = (await store.get(b.id)).assigned_channels
            await eng.dispose()
            return curves, b.definition, a_assigned, b_assigned

        curves, b_definition, a_assigned, b_assigned = run(scenario)
        assert curves == {"pi-pwm-0": b_definition}
        assert a_assigned == ()
        assert b_assigned == ("pi-pwm-0",)

    def test_unassign_returns_true_then_false(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            eng = await fresh()
            store = ScheduleStore(eng)
            s = await store.create(_definition("Moonlight"))
            await store.assign("pi-pwm-1", s.id)
            first = await store.unassign("pi-pwm-1")
            second = await store.unassign("pi-pwm-1")
            await eng.dispose()
            return first, second

        first, second = run(scenario)
        assert first is True
        assert second is False

    def test_assigned_curves_returns_exactly_assigned_map(self) -> None:
        async def scenario() -> tuple[
            dict[str, ScheduleDefinition], ScheduleDefinition, ScheduleDefinition
        ]:
            eng = await fresh()
            store = ScheduleStore(eng)
            a = await store.create(_definition("Blue Channel"))
            b = await store.create(_definition("White Channel"))
            await store.create(_definition("Unused"))  # never assigned
            await store.assign("pi-pwm-0", a.id)
            await store.assign("pi-pwm-1", b.id)
            curves = await store.assigned_curves()
            await eng.dispose()
            return curves, a.definition, b.definition

        curves, a_definition, b_definition = run(scenario)
        assert curves == {"pi-pwm-0": a_definition, "pi-pwm-1": b_definition}


class TestCorruptStoredRow:
    def test_invalid_stored_json_raises_value_error_naming_schedule(self) -> None:
        """A row written outside the store (or by a since-tightened rule)
        whose JSON no longer validates must fail loudly — never be silently
        skipped or returned half-built."""

        async def scenario() -> tuple[UUID, str]:
            eng = await fresh()
            schedule_id = uuid4()
            async with eng.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO lighting_schedules (id, name, points, zone, anchor, locale) "
                        "VALUES (:id, :name, CAST(:points AS JSONB), 'UTC', 'clock', NULL)"
                    ),
                    {
                        "id": schedule_id,
                        "name": "Corrupted Curve",
                        # One point: violates ScheduleDefinition's min_length=2 —
                        # a constraint the JSONB column itself does not enforce.
                        "points": json.dumps([{"at": "08:00:00", "duty": 0.0}]),
                    },
                )
            store = ScheduleStore(eng)
            with pytest.raises(ValueError, match="Corrupted Curve") as excinfo:
                await store.get(schedule_id)
            message = str(excinfo.value)
            await eng.dispose()
            return schedule_id, message

        schedule_id, message = run(scenario)
        assert str(schedule_id) in message

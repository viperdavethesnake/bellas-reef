# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The lighting schedule library: CRUD, assign/unassign, audited.

.superpowers/sdd/2026-08-19-lighting-schedules-backend/task-5-brief.md.
Two error paths look alike on the surface and must not be confused: a bad
curve (one point, duplicate times, a v2-only solar anchor) is 422 — the
request itself is malformed — while a name collision is 409 — the request is
fine, the name is taken. pydantic's ``ValidationError`` subclasses
``ValueError``, which is exactly the trap that would blur the two if the
route folded curve construction and the store call into one try/except.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from bellasreef_api.app import build_app
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"
pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class Audit:
    """Captures the whole call, category included — see
    test_device_binding.py's copy for why dropping the category was a
    blindspot."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any], str]] = []

    async def __call__(self, event: str, detail: dict[str, Any], category: str = "auth") -> None:
        self.records.append((event, detail, category))

    @property
    def events(self) -> list[str]:
        return [e for e, _, _ in self.records]

    def count(self, event: str) -> int:
        return sum(1 for e in self.events if e == event)

    def category(self, event: str) -> str:
        return next(c for e, _, c in self.records if e == event)

    def detail(self, event: str) -> dict[str, Any]:
        return next(d for e, d, _ in self.records if e == event)


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE schedule_assignments, lighting_schedules, "
                "pairing_windows, pairing_requests, paired_clients, devices CASCADE"
            )
        )
    return engine


async def seed_device(engine: AsyncEngine, device_id: str, authority: str) -> None:
    """A device row carrying a given control authority, as
    test_registration_and_naming.py's ``_seed`` does. ``device_id`` doubles
    as the channel id — ``schedule_assignments.channel_id`` has no foreign
    key onto ``devices`` (an unassigned channel is legal before adoption), but
    ``control_authority_of`` looks the id up in ``devices`` by ``device_id``.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO devices (id, device_id, kind, driver_id, actuator_class, "
                "role, control_authority, failsafe_capable, transport, safe_state, "
                "max_runtime_s, heartbeat_timeout_s) VALUES "
                "(gen_random_uuid(), :device_id, 'actuator', 'kessil', 'pwm', 'light', "
                ":authority, :failsafe, :transport, CAST(:safe AS JSONB), :runtime, :beat)"
            ),
            {
                "device_id": device_id,
                "authority": authority,
                "failsafe": authority == "authoritative",
                "transport": "local" if authority == "authoritative" else "network",
                "safe": '{"kind": "pwm", "duty": 0.0}' if authority == "authoritative" else None,
                "runtime": 3600.0 if authority == "authoritative" else None,
                "beat": 15.0 if authority == "authoritative" else None,
            },
        )


async def paired(app: Any) -> dict[str, str]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub"
    ) as c:
        granted = (await c.post("/api/v1/pair", json={"client_name": "phone"})).json()
        tok = (
            await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
        ).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


def curve(name: str = "blue-diurnal", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "points": [
            {"at": "06:00:00", "duty": 0.0},
            {"at": "12:00:00", "duty": 1.0},
            {"at": "20:00:00", "duty": 0.0},
        ],
    }
    body.update(overrides)
    return body


class TestScheduleCRUD:
    def test_create_get_list_round_trip(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            out: dict[str, Any] = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                out["create_code"] = created.status_code
                out["created"] = created.json()

                got = await c.get(
                    f"/api/v1/lighting/schedules/{out['created']['id']}", headers=headers
                )
                out["get_code"] = got.status_code
                out["got"] = got.json()

                listed = await c.get("/api/v1/lighting/schedules", headers=headers)
                out["listed"] = listed.json()
            await engine.dispose()
            return out

        out = run(scenario)
        assert out["create_code"] == 200
        assert out["created"]["name"] == "blue-diurnal"
        assert out["created"]["zone"] == "UTC"
        assert out["created"]["anchor"] == "clock"
        assert out["created"]["locale"] is None
        assert out["created"]["assigned_channels"] == []
        assert len(out["created"]["points"]) == 3
        assert out["get_code"] == 200
        assert out["got"] == out["created"]
        assert [s["id"] for s in out["listed"]] == [out["created"]["id"]]

    def test_get_unknown_is_404(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.get(f"/api/v1/lighting/schedules/{uuid.uuid4()}", headers=headers)
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 404

    def test_update_replaces_full_definition(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            out: dict[str, Any] = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                updated = await c.put(
                    f"/api/v1/lighting/schedules/{created['id']}",
                    headers=headers,
                    json=curve(
                        name="blue-diurnal",
                        points=[{"at": "07:00:00", "duty": 0.2}, {"at": "19:00:00", "duty": 0.0}],
                    ),
                )
                out["update_code"] = updated.status_code
                out["updated"] = updated.json()
            await engine.dispose()
            return out

        out = run(scenario)
        assert out["update_code"] == 200
        assert len(out["updated"]["points"]) == 2
        assert out["updated"]["points"][0]["at"] == "07:00:00"

    def test_update_unknown_is_404(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.put(
                    f"/api/v1/lighting/schedules/{uuid.uuid4()}", headers=headers, json=curve()
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 404

    def test_delete_removes_schedule(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            out: dict[str, Any] = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                deleted = await c.delete(
                    f"/api/v1/lighting/schedules/{created['id']}", headers=headers
                )
                out["delete_code"] = deleted.status_code
                out["listed_after"] = (
                    await c.get("/api/v1/lighting/schedules", headers=headers)
                ).json()
            await engine.dispose()
            return out

        out = run(scenario)
        assert out["delete_code"] == 204
        assert out["listed_after"] == []

    def test_delete_unknown_is_404(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.delete(f"/api/v1/lighting/schedules/{uuid.uuid4()}", headers=headers)
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 404

    def test_schedule_endpoints_require_auth(self) -> None:
        async def scenario() -> tuple[int, int]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                codes = (
                    (await c.get("/api/v1/lighting/schedules")).status_code,
                    (await c.post("/api/v1/lighting/schedules", json=curve())).status_code,
                )
            await engine.dispose()
            return codes

        assert run(scenario) == (401, 401)


class TestInvalidCurveIs422:
    def test_one_point_is_422(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.post(
                    "/api/v1/lighting/schedules",
                    headers=headers,
                    json=curve(points=[{"at": "06:00:00", "duty": 0.5}]),
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 422

    def test_duplicate_times_is_422(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.post(
                    "/api/v1/lighting/schedules",
                    headers=headers,
                    json=curve(
                        points=[
                            {"at": "06:00:00", "duty": 0.0},
                            {"at": "06:00:00", "duty": 1.0},
                        ]
                    ),
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 422

    def test_solar_anchor_is_422(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.post(
                    "/api/v1/lighting/schedules",
                    headers=headers,
                    json=curve(anchor="solar_natural"),
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 422

    def test_invalid_curve_on_update_is_422(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                r = await c.put(
                    f"/api/v1/lighting/schedules/{created['id']}",
                    headers=headers,
                    json=curve(anchor="solar_custom"),
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 422


class TestDuplicateNameIs409:
    def test_create_duplicate_name_is_409_naming_it(self) -> None:
        async def scenario() -> tuple[int, str]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                again = await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
            await engine.dispose()
            return again.status_code, again.text

        code, body = run(scenario)
        assert code == 409
        assert "blue-diurnal" in body

    def test_update_to_a_taken_name_is_409(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve("taken"))
                other = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve("mine"))
                ).json()
                r = await c.put(
                    f"/api/v1/lighting/schedules/{other['id']}",
                    headers=headers,
                    json=curve("taken"),
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 409


class TestDeleteAssignedIs409:
    def test_delete_assigned_is_refused(self) -> None:
        async def scenario() -> tuple[int, str]:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
                r = await c.delete(f"/api/v1/lighting/schedules/{created['id']}", headers=headers)
            await engine.dispose()
            return r.status_code, r.text

        code, body = run(scenario)
        assert code == 409
        assert "led-blue" in body


class TestAssign:
    def test_second_assign_replaces_and_get_shows_it(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            out: dict[str, Any] = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                first = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve("first"))
                ).json()
                second = (
                    await c.post(
                        "/api/v1/lighting/schedules", headers=headers, json=curve("second")
                    )
                ).json()

                assign_first = await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": first["id"]},
                )
                out["assign_first_code"] = assign_first.status_code

                assign_second = await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": second["id"]},
                )
                out["assign_second_code"] = assign_second.status_code
                out["assign_second_body"] = assign_second.json()

                out["first_after"] = (
                    await c.get(f"/api/v1/lighting/schedules/{first['id']}", headers=headers)
                ).json()
                out["second_after"] = (
                    await c.get(f"/api/v1/lighting/schedules/{second['id']}", headers=headers)
                ).json()
            await engine.dispose()
            return out

        out = run(scenario)
        assert out["assign_first_code"] == 200
        assert out["assign_second_code"] == 200
        assert out["assign_second_body"]["assigned_channels"] == ["led-blue"]
        assert out["first_after"]["assigned_channels"] == []
        assert out["second_after"]["assigned_channels"] == ["led-blue"]

    def test_assign_unknown_schedule_is_404(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": str(uuid.uuid4())},
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 404

    def test_assign_to_observe_only_is_409(self) -> None:
        async def scenario() -> tuple[int, str]:
            engine = await fresh_engine()
            await seed_device(engine, "led-observe", "observe_only")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                r = await c.put(
                    "/api/v1/lighting/channels/led-observe/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
            await engine.dispose()
            return r.status_code, r.text

        code, body = run(scenario)
        assert code == 409
        assert "observe_only" in body

    def test_assign_to_unknown_channel_is_legal(self) -> None:
        """Schedule-before-adoption: the channel need not exist yet, the
        engine holds the curve until it is adopted."""

        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                r = await c.put(
                    "/api/v1/lighting/channels/not-yet-adopted/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 200

    def test_assign_malformed_channel_id_is_422(self) -> None:
        """Unknown is legal (test above); malformed is not. The engine's
        ChannelProfile.channel_id validates against this same pattern, so a
        row that skipped this check would only surface as the engine failing
        to build a profile for it on the next reload — see the whole-branch
        review finding this closes."""

        async def scenario() -> tuple[int, str]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                r = await c.put(
                    "/api/v1/lighting/channels/LED-Blue/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
            await engine.dispose()
            return r.status_code, r.text

        code, body = run(scenario)
        assert code == 422
        assert "LED-Blue" in body


class TestUnassign:
    def test_unassign_then_404(self) -> None:
        async def scenario() -> tuple[int, int]:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
                first = await c.delete(
                    "/api/v1/lighting/channels/led-blue/schedule", headers=headers
                )
                second = await c.delete(
                    "/api/v1/lighting/channels/led-blue/schedule", headers=headers
                )
            await engine.dispose()
            return first.status_code, second.status_code

        first_code, second_code = run(scenario)
        assert first_code == 200
        assert second_code == 404

    def test_unassign_removes_from_get(self) -> None:
        async def scenario() -> list[str]:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
                await c.delete("/api/v1/lighting/channels/led-blue/schedule", headers=headers)
                after = (
                    await c.get(f"/api/v1/lighting/schedules/{created['id']}", headers=headers)
                ).json()
            await engine.dispose()
            channels: list[str] = after["assigned_channels"]
            return channels

        assert run(scenario) == []


class TestAudit:
    """Every mutation writes exactly its audit row — under `category="config"`,
    not clock-gated the way an override's deadline is."""

    def test_create_writes_exactly_one_created_event(self) -> None:
        async def scenario() -> Audit:
            engine = await fresh_engine()
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
            await engine.dispose()
            return audit

        audit = run(scenario)
        assert audit.count("schedule.created") == 1
        assert audit.category("schedule.created") == "config"
        detail = audit.detail("schedule.created")
        assert detail["name"] == "blue-diurnal"
        assert "schedule_id" in detail and "actor" in detail

    def test_update_writes_exactly_one_updated_event(self) -> None:
        async def scenario() -> Audit:
            engine = await fresh_engine()
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                await c.put(
                    f"/api/v1/lighting/schedules/{created['id']}", headers=headers, json=curve()
                )
            await engine.dispose()
            return audit

        audit = run(scenario)
        assert audit.count("schedule.updated") == 1
        assert audit.category("schedule.updated") == "config"

    def test_delete_writes_exactly_one_deleted_event(self) -> None:
        async def scenario() -> Audit:
            engine = await fresh_engine()
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                await c.delete(f"/api/v1/lighting/schedules/{created['id']}", headers=headers)
            await engine.dispose()
            return audit

        audit = run(scenario)
        assert audit.count("schedule.deleted") == 1
        assert audit.category("schedule.deleted") == "config"
        detail = audit.detail("schedule.deleted")
        assert detail["name"] == "blue-diurnal"

    def test_assign_and_unassign_write_exactly_their_events(self) -> None:
        async def scenario() -> Audit:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
                await c.delete("/api/v1/lighting/channels/led-blue/schedule", headers=headers)
            await engine.dispose()
            return audit

        audit = run(scenario)
        assert audit.count("schedule.assigned") == 1
        assert audit.count("schedule.unassigned") == 1
        assert audit.category("schedule.assigned") == "config"
        assert audit.category("schedule.unassigned") == "config"
        assigned_detail = audit.detail("schedule.assigned")
        assert assigned_detail["channel_id"] == "led-blue"
        unassigned_detail = audit.detail("schedule.unassigned")
        assert unassigned_detail["channel_id"] == "led-blue"
        assert unassigned_detail["schedule_id"] == assigned_detail["schedule_id"]

    def test_moving_a_channel_writes_unassigned_for_the_old_schedule(self) -> None:
        """Reassigning a channel is a departure and an arrival, and the old
        schedule's history must show the departure (rehearsal follow-up,
        2026-08-25). ``assign`` replaces silently at the store layer, so
        without this row the old schedule's audit trail shows the channel
        arriving and never leaving."""

        async def scenario() -> tuple[Audit, str, str]:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                first = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                second = (
                    await c.post(
                        "/api/v1/lighting/schedules",
                        headers=headers,
                        json=curve(name="amber-dusk"),
                    )
                ).json()
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": first["id"]},
                )
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": second["id"]},
                )
            await engine.dispose()
            return audit, first["id"], second["id"]

        audit, first_id, second_id = run(scenario)
        assert audit.count("schedule.assigned") == 2
        assert audit.count("schedule.unassigned") == 1
        detail = audit.detail("schedule.unassigned")
        assert detail["channel_id"] == "led-blue"
        assert detail["schedule_id"] == first_id, "the row names the OLD schedule"
        assert detail["moved_to"] == second_id

    def test_reassigning_the_same_schedule_writes_no_unassigned(self) -> None:
        """Idempotent re-assign is not a move; no phantom departure row."""

        async def scenario() -> Audit:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                for _ in range(2):
                    await c.put(
                        "/api/v1/lighting/channels/led-blue/schedule",
                        headers=headers,
                        json={"schedule_id": created["id"]},
                    )
            await engine.dispose()
            return audit

        audit = run(scenario)
        assert audit.count("schedule.assigned") == 2
        assert audit.count("schedule.unassigned") == 0

    def test_audit_actor_is_the_client_name_not_a_uuid(self) -> None:
        """Checkpoint D observation (rehearsal 2026-08-24): a bare UUID names
        nobody. ``actor`` carries the paired client's name; ``actor_id`` keeps
        the UUID for identity."""

        async def scenario() -> Audit:
            engine = await fresh_engine()
            await seed_device(engine, "led-blue", "authoritative")
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)  # pairs as "phone"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                ).json()
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
            await engine.dispose()
            return audit

        audit = run(scenario)
        detail = audit.detail("schedule.assigned")
        assert detail["actor"] == "phone"
        uuid.UUID(detail["actor_id"])  # parses, or raises

    def test_failed_mutations_write_no_audit_row(self) -> None:
        """A 409/422/404 must not leave a trail of a mutation that never
        happened."""

        async def scenario() -> Audit:
            engine = await fresh_engine()
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                # duplicate name -> 409
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
                # invalid curve -> 422
                await c.post(
                    "/api/v1/lighting/schedules",
                    headers=headers,
                    json=curve("bad", anchor="solar_natural"),
                )
            await engine.dispose()
            return audit

        audit = run(scenario)
        assert audit.count("schedule.created") == 1

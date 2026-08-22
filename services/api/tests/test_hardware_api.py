# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""`GET /api/v1/hardware` — the Hardware leaf's data source.

Task 4 of the chip-state PR2 plan
(docs/superpowers/plans/2026-08-22-chip-state-pr2-api.md). Same harness shape
as `test_device_binding.py`'s capability tests: a real Postgres-backed app via
`build_app`, no NATS (`nats_url=None`), truncate-and-seed per test. Skipped
without `BELLASREEF_TEST_DATABASE_URL`, never pointed at the hub (see the
environment-boundary rule in CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from bellasreef_api.app import build_app
from bellasreef_api.store import Store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE chip_state"))
        await conn.execute(text("TRUNCATE pairing_requests, paired_clients CASCADE"))
    return engine


async def client_for(engine: AsyncEngine) -> tuple[httpx.AsyncClient, dict[str, str]]:
    app = build_app(engine, nats_url=None, vm_url=None)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")
    granted = (
        await c.post("/api/v1/pair", json={"client_name": f"t-{uuid.uuid4().hex[:6]}"})
    ).json()
    minted = await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
    token = minted.json()["access_token"]
    return c, {"Authorization": f"Bearer {token}"}


def test_hardware_needs_a_bearer() -> None:
    async def scenario() -> int:
        engine = await fresh_engine()
        c, _headers = await client_for(engine)
        try:
            return (await c.get("/api/v1/hardware")).status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 401


def test_hardware_is_empty_on_a_fresh_store() -> None:
    async def scenario() -> list[dict[str, Any]]:
        engine = await fresh_engine()
        c, headers = await client_for(engine)
        try:
            resp = await c.get("/api/v1/hardware", headers=headers)
            assert resp.status_code == 200
            body: list[dict[str, Any]] = resp.json()
            return body
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == []


def test_hardware_returns_seeded_rows_ordered_with_facts_intact() -> None:
    """Seeded out of order; the endpoint is the ordering authority (source,
    then instance), same reasoning as `/api/v1/capabilities`, and every
    facts value type (str/int/float/bool) survives the round trip."""

    async def scenario() -> list[dict[str, Any]]:
        engine = await fresh_engine()
        store = Store(engine)

        await store.upsert_chip_state(
            source="wifi",
            instance="b",
            initialised=True,
            initialised_at=datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
            facts={"rssi_dbm": -42},
            announced_at=datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
        )
        await store.upsert_chip_state(
            source="pca9685",
            instance="0x40",
            initialised=True,
            initialised_at=datetime(2026, 8, 22, 12, 0, 5, tzinfo=UTC),
            facts={
                "chip": "pca9685",
                "pre_scale": 12,
                "frequency_hz": 502.7,
                "invrt": False,
            },
            announced_at=datetime(2026, 8, 22, 12, 5, 0, tzinfo=UTC),
        )
        await store.upsert_chip_state(
            source="pca9685",
            instance="0x41",
            initialised=False,
            initialised_at=None,
            facts={},
            announced_at=datetime(2026, 8, 22, 12, 5, 1, tzinfo=UTC),
        )

        c, headers = await client_for(engine)
        try:
            resp = await c.get("/api/v1/hardware", headers=headers)
            assert resp.status_code == 200
            body: list[dict[str, Any]] = resp.json()
            return body
        finally:
            await c.aclose()
            await engine.dispose()

    rows = run(scenario)
    assert [(r["source"], r["instance"]) for r in rows] == [
        ("pca9685", "0x40"),
        ("pca9685", "0x41"),
        ("wifi", "b"),
    ]

    first = rows[0]
    assert first["initialised"] is True
    assert first["initialised_at"] == "2026-08-22T12:00:05Z"
    assert first["facts"] == {
        "chip": "pca9685",
        "pre_scale": 12,
        "frequency_hz": 502.7,
        "invrt": False,
    }
    assert first["announced_at"] == "2026-08-22T12:05:00Z"

    uninitialised = rows[1]
    assert uninitialised["initialised"] is False
    assert uninitialised["initialised_at"] is None
    assert uninitialised["facts"] == {}


def test_hardware_operation_id_is_published_in_the_openapi_schema() -> None:
    async def scenario() -> dict[str, Any]:
        engine = await fresh_engine()
        app = build_app(engine, nats_url=None, vm_url=None)
        try:
            schema: dict[str, Any] = app.openapi()
            return schema
        finally:
            await engine.dispose()

    schema = run(scenario)
    op = schema["paths"]["/api/v1/hardware"]["get"]
    assert op["operationId"] == "listHardware"

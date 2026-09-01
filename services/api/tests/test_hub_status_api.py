# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""`GET /api/v1/hub-status` — the Hub status leaf's data source.

Same harness shape as `test_hardware_api.py`: a real Postgres-backed app via
`build_app`, no NATS (`nats_url=None`), truncate per test. The snapshot
itself is injected through the `app.state.background` seam, because with no
NATS there is no consumer — which is also the 404 path a pre-4.3.0 hub or a
fresh boot serves. Skipped without `BELLASREEF_TEST_DATABASE_URL`, never
pointed at the hub (environment-boundary rule, CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from bellasreef_api.app import build_app
from bellasreef_contracts import HostStatus
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


_NOW = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)


def _status() -> HostStatus:
    return HostStatus(
        message_id=uuid4(),
        emitted_at=_NOW,
        source="hardware-io",
        load_1m=0.42,
        load_5m=0.38,
        load_15m=0.33,
        cpu_count=4,
        mem_total_kb=1014464,
        mem_available_kb=445792,
        temp_c=46.3,
        uptime_s=1692.78,
    )


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE pairing_requests, paired_clients CASCADE"))
    return engine


async def client_for(
    engine: AsyncEngine,
) -> tuple[httpx.AsyncClient, dict[str, str], Any]:
    app = build_app(engine, nats_url=None, vm_url=None)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")
    granted = (
        await c.post("/api/v1/pair", json={"client_name": f"t-{uuid.uuid4().hex[:6]}"})
    ).json()
    minted = await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
    token = minted.json()["access_token"]
    return c, {"Authorization": f"Bearer {token}"}, app


def test_hub_status_needs_a_bearer() -> None:
    async def scenario() -> int:
        engine = await fresh_engine()
        c, _headers, _app = await client_for(engine)
        try:
            return (await c.get("/api/v1/hub-status")).status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 401


def test_hub_status_is_404_before_the_first_message() -> None:
    async def scenario() -> int:
        engine = await fresh_engine()
        c, headers, _app = await client_for(engine)
        try:
            return (await c.get("/api/v1/hub-status", headers=headers)).status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 404


def test_hub_status_serves_the_latest_snapshot() -> None:
    async def scenario() -> dict[str, Any]:
        engine = await fresh_engine()
        c, headers, app = await client_for(engine)
        app.state.background["host status consumer"] = SimpleNamespace(latest=_status())
        try:
            resp = await c.get("/api/v1/hub-status", headers=headers)
            assert resp.status_code == 200, resp.text
            body: dict[str, Any] = resp.json()
            return body
        finally:
            await c.aclose()
            await engine.dispose()

    body = run(scenario)
    assert body["load_1m"] == 0.42
    assert body["cpu_count"] == 4
    assert body["mem_total_kb"] == 1014464
    assert body["mem_available_kb"] == 445792
    assert body["temp_c"] == 46.3
    assert body["uptime_s"] == 1692.78
    assert body["updated_at"] == "2026-08-31T21:00:00Z"

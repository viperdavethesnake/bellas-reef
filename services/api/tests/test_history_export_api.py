# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""`GET /api/v1/history/export` — the guards, before any samples are read.

Integration-shaped, and for one reason: `build_app` takes a real engine and the
route asks `store.list_devices()` which device it was handed, so there is no
Postgres-free construction of the app to test against. Every case here refuses
the request before VictoriaMetrics is ever called, which is why a dummy
`vm_url` is enough — the cases that read real samples live in `test_history.py`
alongside the rest of the VictoriaMetrics round trips.

Skipped without `BELLASREEF_TEST_DATABASE_URL`, never pointed at the hub
(environment-boundary rule, CLAUDE.md).
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

#: Unreachable on purpose. A case that reaches it is a case that was supposed
#: to have been refused by a guard.
_UNREACHABLE_VM = "http://127.0.0.1:1"


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE sensor_alerts, overrides, pairing_windows, pairing_requests, "
                "paired_clients, devices CASCADE"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO devices (id, device_id, kind, driver_id, sensor_type, "
                "poll_interval_s, transport) VALUES (:id, 'display-tank', 'sensor', "
                "'ds18b20', 'temp', 5.0, 'local')"
            ),
            {"id": uuid.uuid4()},
        )
    return engine


async def paired(app: Any) -> dict[str, str]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub"
    ) as c:
        granted = (await c.post("/api/v1/pair", json={"client_name": "phone"})).json()
        tok = (
            await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
        ).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


def export(
    params: dict[str, str], *, vm_url: str | None = _UNREACHABLE_VM, authenticate: bool = True
) -> httpx.Response:
    async def scenario() -> httpx.Response:
        engine = await fresh_engine()
        app = build_app(engine, vm_url=vm_url)
        headers = await paired(app) if authenticate else {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            response = await c.get("/api/v1/history/export", params=params, headers=headers)
        await engine.dispose()
        return response

    return run(scenario)


def window(start: str, end: str, device_id: str = "display-tank") -> dict[str, str]:
    return {"device_id": device_id, "start": start, "end": end}


class TestWindowGuards:
    """Same guards as `/api/v1/history`, plus the cap this route adds.

    A naive datetime reaches the comparison below otherwise, which raises
    TypeError against an aware one and silently means server-local time when
    both are naive — a 500 or a wrong window, where the route promises a 422.
    """

    def test_a_naive_start_is_refused(self) -> None:
        got = export(window("2026-08-10T12:00:00", "2026-08-10T13:00:00Z"))
        assert got.status_code == 422, got.text

    def test_a_naive_end_is_refused(self) -> None:
        got = export(window("2026-08-10T12:00:00Z", "2026-08-10T13:00:00"))
        assert got.status_code == 422, got.text

    def test_an_end_before_the_start_is_refused(self) -> None:
        got = export(window("2026-08-10T13:00:00Z", "2026-08-10T12:00:00Z"))
        assert got.status_code == 422, got.text

    def test_an_equal_start_and_end_is_refused(self) -> None:
        got = export(window("2026-08-10T12:00:00Z", "2026-08-10T12:00:00Z"))
        assert got.status_code == 422, got.text

    def test_a_window_longer_than_31_days_is_refused(self) -> None:
        """The cap bounds the response: the whole window is rendered in memory,
        and a probe at the default cadence is tens of thousands of rows a day."""
        got = export(window("2026-07-01T00:00:00Z", "2026-08-02T00:00:00Z"))
        assert got.status_code == 422, got.text
        assert "31 days" in got.text

    def test_exactly_31_days_is_accepted(self) -> None:
        """The boundary is inclusive. A month-long window is the case the
        feature exists for, and an off-by-one here would refuse it.

        Asserted through the device guard, which runs after the window guards:
        a 404 means the window was accepted and the request got as far as
        looking the device up. Reading samples needs VictoriaMetrics, so the
        200 path is exercised in `test_history.py`.
        """
        got = export(window("2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z", "no-such-probe"))
        assert got.status_code == 404, got.text


class TestDeviceGuard:
    def test_an_unregistered_device_is_a_404(self) -> None:
        """Not an empty file. A typo in a device id would otherwise download as
        a valid export of nothing, which reads as "the probe recorded nothing"."""
        got = export(window("2026-08-10T12:00:00Z", "2026-08-10T13:00:00Z", "no-such-probe"))
        assert got.status_code == 404, got.text
        assert "unknown device" in got.text


class TestAuth:
    def test_the_export_needs_a_bearer(self) -> None:
        got = export(window("2026-08-10T12:00:00Z", "2026-08-10T13:00:00Z"), authenticate=False)
        assert got.status_code == 401, got.text


class TestNoTelemetryStore:
    def test_a_hub_without_victoriametrics_says_so(self) -> None:
        """503, exactly like `/api/v1/history`. There is nothing to export from
        and no amount of retrying will change that."""
        got = export(window("2026-08-10T12:00:00Z", "2026-08-10T13:00:00Z"), vm_url=None)
        assert got.status_code == 503, got.text

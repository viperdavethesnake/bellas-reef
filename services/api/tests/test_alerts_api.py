# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Threshold configuration and alert history endpoints (PRD R12)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
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
    def __init__(self) -> None:
        self.events: list[str] = []

    async def __call__(self, event: str, detail: dict[str, Any], category: str = "auth") -> None:
        self.events.append(event)


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
        await conn.execute(
            text(
                # Authoritative: a PCA9685 channel is one we actually drive
                # (docs/device-classes.md §2.1).
                "INSERT INTO devices (id, device_id, kind, driver_id, actuator_class, "
                "role, control_authority, failsafe_capable, transport, safe_state, "
                "max_runtime_s, heartbeat_timeout_s) VALUES "
                "(:id, 'led-blue', 'actuator', 'pca9685', 'pwm', 'light', "
                "'authoritative', true, 'local', CAST(:safe AS JSONB), 3600, 15)"
            ),
            {"id": uuid.uuid4(), "safe": '{"kind": "pwm", "duty": 0.0}'},
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


async def seed_episode(engine: AsyncEngine, *, cleared: bool) -> None:
    raised = datetime.now(UTC) - timedelta(minutes=10)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sensor_alerts (id, device_id, sensor_type, bound, threshold, "
                "clear_margin, unit, raised_at, raised_value, cleared_at, cleared_value) "
                "VALUES (:id, 'display-tank', 'temp', 'min', 24.0, 0.5, 'degC', :raised, "
                "23.1, :cleared_at, :cleared_value)"
            ),
            {
                "id": uuid.uuid4(),
                "raised": raised,
                "cleared_at": raised + timedelta(minutes=2) if cleared else None,
                "cleared_value": 24.7 if cleared else None,
            },
        )


class TestThresholdConfiguration:
    def test_a_sensor_starts_with_no_band(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                body: dict[str, Any] = (
                    await c.get("/api/v1/devices/display-tank/thresholds", headers=headers)
                ).json()
            await engine.dispose()
            return body

        thresholds = run(scenario)
        assert thresholds == {
            "device_id": "display-tank",
            "minimum": None,
            "maximum": None,
            "clear_margin": None,
        }

    def test_setting_and_reading_back_a_band(self) -> None:
        async def scenario() -> tuple[int, dict[str, Any], list[str]]:
            engine = await fresh_engine()
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                put = await c.put(
                    "/api/v1/devices/display-tank/thresholds",
                    headers=headers,
                    json={"minimum": 24.0, "maximum": 27.0, "clear_margin": 0.5},
                )
                got: dict[str, Any] = (
                    await c.get("/api/v1/devices/display-tank/thresholds", headers=headers)
                ).json()
            await engine.dispose()
            return put.status_code, got, audit.events

        code, thresholds, events = run(scenario)
        assert code == 200
        assert (thresholds["minimum"], thresholds["maximum"], thresholds["clear_margin"]) == (
            24.0,
            27.0,
            0.5,
        )
        # Changing what the tank is allowed to do is an operator action, so it
        # belongs in the audit trail alongside pairing and overrides.
        assert "thresholds.set" in events

    def test_clearing_every_field_turns_alerting_off(self) -> None:
        """The only way to say "stop watching this" without a separate verb."""

        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                await c.put(
                    "/api/v1/devices/display-tank/thresholds",
                    headers=headers,
                    json={"minimum": 24.0, "maximum": 27.0, "clear_margin": 0.5},
                )
                await c.put("/api/v1/devices/display-tank/thresholds", headers=headers, json={})
                body: dict[str, Any] = (
                    await c.get("/api/v1/devices/display-tank/thresholds", headers=headers)
                ).json()
            await engine.dispose()
            return body

        assert run(scenario)["minimum"] is None

    @pytest.mark.parametrize(
        ("payload", "why"),
        [
            ({"minimum": 24.0}, "a threshold without a margin has no clear point"),
            ({"minimum": 27.0, "maximum": 24.0, "clear_margin": 0.5}, "inverted band"),
            ({"minimum": 24.0, "maximum": 25.0, "clear_margin": 0.6}, "unreachable clear zone"),
            ({"minimum": 24.0, "maximum": 27.0, "clear_margin": 0.0}, "non-positive margin"),
        ],
    )
    def test_an_unusable_band_is_refused_with_422(self, payload: dict[str, Any], why: str) -> None:
        """422 naming a field, not a 500 naming a constraint.

        The database would reject all four of these anyway; the point of the
        model validator is that the operator gets told which field is wrong.
        """

        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.put(
                    "/api/v1/devices/display-tank/thresholds", headers=headers, json=payload
                )
            await engine.dispose()
            return response.status_code

        assert run(scenario) == 422, why

    def test_thresholds_on_an_actuator_are_refused(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.put(
                    "/api/v1/devices/led-blue/thresholds",
                    headers=headers,
                    json={"minimum": 24.0, "maximum": 27.0, "clear_margin": 0.5},
                )
            await engine.dispose()
            return response.status_code

        assert run(scenario) == 409

    def test_an_unknown_device_is_404_not_a_silent_create(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.put(
                    "/api/v1/devices/no-such-probe/thresholds",
                    headers=headers,
                    json={"minimum": 24.0, "maximum": 27.0, "clear_margin": 0.5},
                )
            await engine.dispose()
            return response.status_code

        assert run(scenario) == 404

    def test_thresholds_require_authentication(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.get("/api/v1/devices/display-tank/thresholds")
            await engine.dispose()
            return response.status_code

        assert run(scenario) == 401


class TestAlertHistory:
    def test_an_open_episode_is_active_and_recent(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            await seed_episode(engine, cleared=False)
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                body: dict[str, Any] = (await c.get("/api/v1/alerts", headers=headers)).json()
            await engine.dispose()
            return body

        alerts = run(scenario)
        assert len(alerts["active"]) == 1
        assert len(alerts["recent"]) == 1
        assert alerts["active"][0]["bound"] == "min"
        assert alerts["active"][0]["cleared_at"] is None

    def test_a_cleared_episode_leaves_active_but_stays_in_recent(self) -> None:
        """This is the reconnect path: a client that was asleep during a breach
        learns it happened here, because alerts are not replayed on the spine."""

        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            await seed_episode(engine, cleared=True)
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                body: dict[str, Any] = (await c.get("/api/v1/alerts", headers=headers)).json()
            await engine.dispose()
            return body

        alerts = run(scenario)
        assert alerts["active"] == []
        assert len(alerts["recent"]) == 1
        assert alerts["recent"][0]["cleared_value"] == 24.7

    def test_alerts_require_authentication(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.get("/api/v1/alerts")
            await engine.dispose()
            return response.status_code

        assert run(scenario) == 401

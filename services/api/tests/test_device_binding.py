# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Binding a capability to a device, and the fork it must never produce again.

A seed naming a probe ``display-tank`` created a second device row beside the
one already holding that probe's name, its alert thresholds, its episode history
and a day of telemetry. The tank's history forked in two and kept running that
way for over an hour.

Nothing about that was subtle in hindsight. The ROM is the hardware's identity;
the ``device_id`` is the registry's; and a caller proposing a new id for
hardware already in the registry is renaming at most, never creating. The seed
path did not know that, so it created.

These tests are that rule, plus the three validations that stop a binding being
accepted at all.
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
from bellasreef_api.store import Store
from bellasreef_contracts import LIGHT_HEARTBEAT_TIMEOUT_S, LIGHT_MAX_RUNTIME_S
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")

ROM = "28-00000000beef"


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class Audit:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def __call__(self, event: str, detail: dict[str, Any]) -> None:
        self.events.append(event)


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sensor_alerts, devices, capabilities CASCADE"))
        await conn.execute(text("TRUNCATE pairing_requests, paired_clients CASCADE"))
    return engine


async def announce(engine: AsyncEngine, source: str, channel: str) -> None:
    """Put a capability in the registry, as hardware-io would."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO capabilities (id, source, channel, detail) "
                "VALUES (:id, :source, :channel, '{}'::jsonb) "
                "ON CONFLICT (source, channel) DO NOTHING"
            ),
            {"id": uuid.uuid4(), "source": source, "channel": channel},
        )


async def client_for(engine: AsyncEngine) -> tuple[httpx.AsyncClient, dict[str, str]]:
    app = build_app(engine, audit=Audit(), nats_url=None, vm_url=None)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")
    granted = await c.post("/api/v1/pair", json={"client_name": f"t-{uuid.uuid4().hex[:6]}"})
    token = (
        await c.post("/api/v1/token", json={"refresh_token": granted.json()["refresh_token"]})
    ).json()["access_token"]
    return c, {"Authorization": f"Bearer {token}"}


async def device_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM devices"))).scalar_one())


# ------------------------------------------------------- the fork, prevented


def test_seeding_over_known_hardware_creates_no_new_rows() -> None:
    """The regression test for the identity fork.

    A device already holds this ROM, with a name and thresholds an operator set.
    A seed arrives proposing a different id for the same physical probe. It must
    adopt what is there — not create beside it.
    """

    async def scenario() -> tuple[int, dict[str, Any], dict[str, Any]]:
        engine = await fresh_engine()
        await announce(engine, "w1-bus", ROM)

        # The device as it exists today: named by the operator, thresholds set,
        # history behind it.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO devices (id, device_id, kind, driver_id, sensor_type, "
                    "  poll_interval_s, transport, display_name, alert_min, alert_max, "
                    "  alert_clear_margin, driver_type, binding, adopted) "
                    "VALUES (:id, :did, 'sensor', 'ds18b20', 'temp', 5.0, 'local', "
                    "  'Bob''s Big Ass Tank', 23.0, 25.5, 0.2, 'ds18b20', "
                    "  CAST(:binding AS jsonb), true)"
                ),
                {
                    "id": uuid.uuid4(),
                    "did": f"ds18b20-{ROM}",
                    "binding": f'{{"rom": "{ROM}"}}',
                },
            )

        c, headers = await client_for(engine)
        try:
            before = await device_count(engine)
            response = await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "display-tank",  # the seed's proposed name
                    "driver_type": "ds18b20",
                    "channel": ROM,
                    "poll_interval_s": 5.0,
                },
            )
            after = await device_count(engine)

            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT device_id, display_name, alert_min, alert_max FROM devices")
                    )
                ).mappings()
                surviving = dict(next(iter(row)))

            return after - before, response.json(), surviving
        finally:
            await c.aclose()
            await engine.dispose()

    new_rows, body, surviving = run(scenario)

    assert new_rows == 0, (
        "the seed created a device beside hardware already in the registry. This is "
        "the fork: two rows for one probe, and a tank's history running down both."
    )
    assert body["created"] is False
    assert body["device_id"] == f"ds18b20-{ROM}", "the registry's id wins, not the seed's"
    assert surviving["display_name"] == "Bob's Big Ass Tank", "the operator's name survived"
    assert surviving["alert_min"] == 23.0, "the operator's thresholds survived"
    assert surviving["alert_max"] == 25.5


def test_rebinding_the_same_device_is_idempotent() -> None:
    """A seed re-run changes nothing. Running it twice is how seeds get used."""

    async def scenario() -> tuple[int, int]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            payload = {
                "device_id": "led-blue",
                "driver_type": "pi-pwm",
                "channel": "0",
                "role": "light",
            }
            await c.post("/api/v1/devices", headers=headers, json=payload)
            first = await device_count(engine)
            await c.post("/api/v1/devices", headers=headers, json=payload)
            return first, await device_count(engine)
        finally:
            await c.aclose()
            await engine.dispose()

    first, second = run(scenario)
    assert first == 1
    assert second == 1


def test_a_rebind_does_not_blank_an_operators_name() -> None:
    """A seed that omits display_name must not erase one somebody typed.

    Same rule as the sensor upsert: a re-announce cannot reset the operator's
    choices.
    """

    async def scenario() -> str | None:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "1")
        c, headers = await client_for(engine)
        try:
            await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-white",
                    "driver_type": "pi-pwm",
                    "channel": "1",
                    "role": "light",
                    "display_name": "White",
                },
            )
            await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-white",
                    "driver_type": "pi-pwm",
                    "channel": "1",
                    "role": "light",
                },
            )
            async with engine.connect() as conn:
                return str(
                    (await conn.execute(text("SELECT display_name FROM devices"))).scalar_one()
                )
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == "White"


# --------------------------------------------------------- the three validations


def test_a_probe_binds_against_the_bus_not_a_source_of_its_own() -> None:
    """A DS18B20 is a probe on the w1-bus, and the two names differ.

    Looking up source='ds18b20' would 404 every probe on a hub that is working
    perfectly — caught by CI before it reached the tank.
    """

    async def scenario() -> int:
        engine = await fresh_engine()
        await announce(engine, "w1-bus", ROM)
        c, headers = await client_for(engine)
        try:
            response = await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "probe",
                    "driver_type": "ds18b20",
                    "channel": ROM,
                    "poll_interval_s": 5.0,
                },
            )
            return response.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 200


def test_a_bound_light_declares_its_safety_contract() -> None:
    """The devices CHECK requires it, and the values come from the contract.

    A light bound through the API must satisfy the same constraint as one
    registered by hardware-io — otherwise the API can write a row the safety
    framework would have refused.
    """

    async def scenario() -> dict[str, Any]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-blue",
                    "driver_type": "pi-pwm",
                    "channel": "0",
                    "role": "light",
                },
            )
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT control_authority, failsafe_capable, transport, "
                            "       safe_state, max_runtime_s, heartbeat_timeout_s "
                            "  FROM devices WHERE device_id = 'led-blue'"
                        )
                    )
                ).mappings()
                return dict(next(iter(row)))
        finally:
            await c.aclose()
            await engine.dispose()

    row = run(scenario)
    assert row["control_authority"] == "authoritative"
    assert row["failsafe_capable"] is True
    assert row["transport"] == "local"
    assert row["safe_state"] == {"kind": "pwm", "duty": 0.0}
    assert row["max_runtime_s"] == LIGHT_MAX_RUNTIME_S
    assert row["heartbeat_timeout_s"] == LIGHT_HEARTBEAT_TIMEOUT_S


def test_binding_unannounced_hardware_is_refused() -> None:
    """A device bound to hardware nobody reported can never work."""

    async def scenario() -> int:
        engine = await fresh_engine()
        c, headers = await client_for(engine)
        try:
            response = await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-blue",
                    "driver_type": "pi-pwm",
                    "channel": "0",
                    "role": "light",
                },
            )
            return response.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 404


def test_double_binding_a_channel_is_refused() -> None:
    """Two devices on one channel interleave rather than coexist."""

    async def scenario() -> tuple[int, str]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-blue",
                    "driver_type": "pi-pwm",
                    "channel": "0",
                    "role": "light",
                },
            )
            response = await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-red",
                    "driver_type": "pi-pwm",
                    "channel": "0",
                    "role": "light",
                },
            )
            return response.status_code, response.text
        finally:
            await c.aclose()
            await engine.dispose()

    code, body = run(scenario)
    assert code == 409
    assert "led-blue" in body, "say which device holds it; the operator has to go find it"


def test_an_actuator_without_a_role_is_refused() -> None:
    async def scenario() -> int:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            response = await c.post(
                "/api/v1/devices",
                headers=headers,
                json={"device_id": "led-blue", "driver_type": "pi-pwm", "channel": "0"},
            )
            return response.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 422


def test_a_reserved_role_is_refused_by_the_schema() -> None:
    """`heater` is in the contract and not implemented.

    Accepting it registers a device nothing knows how to drive — and for a
    heater specifically, that is the actuator the PRD says waits for relay
    drivers and passed drills.
    """

    async def scenario() -> int:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            response = await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "heater-main",
                    "driver_type": "pi-pwm",
                    "channel": "0",
                    "role": "heater",
                },
            )
            return response.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 422


def test_a_sensor_carrying_a_role_is_refused() -> None:
    """sensor_type already says what a probe is."""

    async def scenario() -> int:
        engine = await fresh_engine()
        await announce(engine, "w1-bus", ROM)
        c, headers = await client_for(engine)
        try:
            response = await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "probe",
                    "driver_type": "ds18b20",
                    "channel": ROM,
                    "role": "light",
                    "poll_interval_s": 5.0,
                },
            )
            return response.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 422


# ------------------------------------------------------------------ adoption


def test_an_announced_probe_is_adopted_not_duplicated() -> None:
    """1-Wire announces itself; adoption is the operator claiming it.

    The row exists from the announcement with no binding and no name. Adopting
    binds it in place — it must not become a second device.
    """

    async def scenario() -> tuple[int, dict[str, Any]]:
        engine = await fresh_engine()
        await announce(engine, "w1-bus", ROM)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO devices (id, device_id, kind, driver_id, sensor_type, "
                    "  poll_interval_s, transport) "
                    "VALUES (:id, :did, 'sensor', 'ds18b20', 'temp', 5.0, 'local')"
                ),
                {"id": uuid.uuid4(), "did": f"ds18b20-{ROM}"},
            )

        c, headers = await client_for(engine)
        try:
            await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": f"ds18b20-{ROM}",
                    "driver_type": "ds18b20",
                    "channel": ROM,
                    "display_name": "Sump",
                    "poll_interval_s": 5.0,
                },
            )
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(text("SELECT device_id, display_name, adopted FROM devices"))
                ).mappings()
                all_rows = [dict(r) for r in rows]
            return len(all_rows), all_rows[0]
        finally:
            await c.aclose()
            await engine.dispose()

    count, row = run(scenario)
    assert count == 1, "adoption created a second device instead of claiming the one there"
    assert row["adopted"] is True
    assert row["display_name"] == "Sump"


# ------------------------------------------------- assignments survive a restore


def test_only_adopted_bound_devices_are_reconciled() -> None:
    """What gets republished at startup, and what must not.

    Postgres is the source of device topology; the retained assignment stream
    is a cache of it. This is the query that makes that true — without it the
    stream is written once at bind time and never reconciled, so a restored or
    purged stream leaves hardware-io building nothing while the devices table
    looks perfect.

    That is not hypothetical. The R14 archive carries Postgres and deliberately
    omits JetStream, on the grounds that hardware announces itself on boot —
    true for registrations, false for assignments, which only the API publishes
    and only when someone binds. Restore onto fresh hardware without this and
    the tank stays dark, which is the exact scenario R14 exists for.

    An unadopted device must NOT appear: announce-then-adopt means a probe the
    hub can see but nobody has claimed stays inert, and reconciliation is
    exactly the moment that rule would be quietly undone.
    """

    async def scenario() -> list[str]:
        engine = await fresh_engine()
        await announce(engine, "w1-bus", ROM)
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-blue",
                    "driver_type": "pi-pwm",
                    "channel": "0",
                    "role": "light",
                },
            )
            # Announced but never claimed — the inert state.
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO devices (id, device_id, kind, driver_id, sensor_type, "
                        "  poll_interval_s, transport) "
                        "VALUES (:id, 'unclaimed-probe', 'sensor', 'ds18b20', 'temp', 5.0, "
                        "        'local')"
                    ),
                    {"id": uuid.uuid4()},
                )

            store = Store(engine)
            return [row["device_id"] for row in await store.adopted_assignments()]
        finally:
            await c.aclose()
            await engine.dispose()

    reconciled = run(scenario)
    assert reconciled == ["led-blue"]
    assert "unclaimed-probe" not in reconciled, (
        "an unadopted device would be built by hardware-io on the next restart, "
        "undoing announce-then-adopt at exactly the moment nobody is watching"
    )


def test_a_reconciled_assignment_carries_what_the_driver_needs() -> None:
    """Enough to rebuild the driver, or the republish is decorative."""

    async def scenario() -> dict[str, Any]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "1")
        c, headers = await client_for(engine)
        try:
            await c.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "device_id": "led-white",
                    "driver_type": "pi-pwm",
                    "channel": "1",
                    "role": "light",
                },
            )
            return (await Store(engine).adopted_assignments())[0]
        finally:
            await c.aclose()
            await engine.dispose()

    row = run(scenario)
    assert row["driver_type"] == "pi-pwm"
    assert row["binding"] == {"channel": "1"}
    assert row["role"] == "light"

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
from typing import Any, ClassVar

import httpx
import pytest
from bellasreef_api.app import build_app
from bellasreef_api.store import Store
from bellasreef_contracts import LIGHT_HEARTBEAT_TIMEOUT_S, LIGHT_MAX_RUNTIME_S
from bellasreef_contracts.messages import DeviceAssignment
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")

ROM = "28-00000000beef"


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class Audit:
    """Captures the whole call, category included.

    The category used to be accepted and dropped, so a device event that lost
    its ``category="config"`` — and landed in the auth trail — passed here.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any], str]] = []

    async def __call__(self, event: str, detail: dict[str, Any], category: str = "auth") -> None:
        self.records.append((event, detail, category))

    @property
    def events(self) -> list[str]:
        return [e for e, _, _ in self.records]

    def category(self, event: str) -> str:
        """The category the first occurrence of ``event`` was filed under."""
        return next(c for e, _, c in self.records if e == event)


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


async def client_for(
    engine: AsyncEngine, *, audit: Audit | None = None, nats_url: str | None = None
) -> tuple[httpx.AsyncClient, dict[str, str]]:
    app = build_app(engine, audit=audit or Audit(), nats_url=nats_url, vm_url=None)
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


# ------------------------------------------------------------------ unbinding


class RecordingPublisher:
    """Stands in for :class:`AssignmentPublisher`. Records, does not connect."""

    published: ClassVar[list[DeviceAssignment]] = []

    def __init__(self, url: str) -> None:
        self.url = url

    async def publish(self, assignment: DeviceAssignment) -> None:
        RecordingPublisher.published.append(assignment)

    async def close(self) -> None:
        return None


async def bind_light(
    c: httpx.AsyncClient, headers: dict[str, str], device_id: str, channel: str
) -> httpx.Response:
    return await c.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "device_id": device_id,
            "driver_type": "pi-pwm",
            "channel": channel,
            "role": "light",
        },
    )


async def get_device(
    c: httpx.AsyncClient, headers: dict[str, str], device_id: str
) -> dict[str, Any]:
    body: list[dict[str, Any]] = (await c.get("/api/v1/devices", headers=headers)).json()
    return next(d for d in body if d["device_id"] == device_id)


def test_a_bound_devices_channel_is_surfaced() -> None:
    """Two adopted lights are indistinguishable except by name without this.

    David's ruling 2026-08-13: `DeviceView` carries the physical channel its
    binding claims, additive and optional, so a client can finally tell two
    adopted PWM lights apart.
    """

    async def scenario() -> dict[str, Any]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "led-blue", "0")
            return await get_device(c, headers, "led-blue")
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario)["channel"] == "0"


def test_an_unbound_devices_channel_is_none() -> None:
    """A released binding must not leave its old channel visible on the API.

    `test_unbinding_keeps_the_row_and_its_history` pins that the `binding`
    column itself is retained internally so re-binding recognises the same
    hardware — but that is a store detail, not something the API should show
    as a still-claimed channel.
    """

    async def scenario() -> dict[str, Any]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "led-blue", "0")
            await c.delete("/api/v1/devices/led-blue", headers=headers)
            return await get_device(c, headers, "led-blue")
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario)["channel"] is None


def test_unbinding_frees_the_channel_to_be_bound_again() -> None:
    """The lockout this endpoint closes.

    A PWM channel bound to the wrong device was taken for good: `bindDevice`
    returns 409 on an actuator channel somebody else holds, and nothing anywhere
    could let go of one. SQL on the hub was the only way out.
    """

    async def scenario() -> tuple[int, int, int, dict[str, Any]]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "led-blue", "0")
            blocked = await bind_light(c, headers, "led-red", "0")
            unbound = await c.delete("/api/v1/devices/led-blue", headers=headers)
            rebound = await bind_light(c, headers, "led-red", "0")
            return (
                blocked.status_code,
                unbound.status_code,
                rebound.status_code,
                rebound.json(),
            )
        finally:
            await c.aclose()
            await engine.dispose()

    blocked, unbound, rebound, body = run(scenario)
    assert blocked == 409
    assert unbound == 204
    assert rebound == 200

    # Pinned, not endorsed. `Store.bind_device` matches an existing row on
    # (driver_type, channel) whether or not it is adopted, so the freed channel
    # re-adopts the row that used to hold it and the proposed `led-red` is
    # discarded. For a PROBE that is exactly right — the ROM is the hardware's
    # identity. For a PWM channel it is not obviously right: the slot has no
    # identity of its own, and the operator's declaration is the only thing that
    # says what is plugged into it.
    #
    # Left as-is deliberately. Changing the match rule means changing the code
    # path that produced the identity fork, and it cannot be verified without a
    # database. The channel is free either way, which is the lockout this
    # endpoint exists to close. Recorded here so the next person meets it as a
    # decision rather than a surprise.
    assert body["device_id"] == "led-blue"
    assert body["created"] is False


def test_unbinding_keeps_the_row_and_its_history() -> None:
    """Soft, and the softness is the point.

    Deleting the row would sever a device's telemetry, thresholds and alert
    history from the hardware that produced them, and re-binding the same probe
    tomorrow would look like new hardware. That is the identity fork, reached
    from the other end.
    """

    async def scenario() -> tuple[int, dict[str, Any]]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "led-blue", "0")
            await c.patch(
                "/api/v1/devices/led-blue",
                headers=headers,
                json={"display_name": "Left blue bank"},
            )
            await c.delete("/api/v1/devices/led-blue", headers=headers)
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT adopted, display_name, binding FROM devices "
                            " WHERE device_id = 'led-blue'"
                        )
                    )
                ).mappings()
                first = row.first()
                assert first is not None
                return await device_count(engine), dict(first)
        finally:
            await c.aclose()
            await engine.dispose()

    count, row = run(scenario)
    assert count == 1, "the row survives; only its claim on the channel does not"
    assert row["adopted"] is False
    assert row["display_name"] == "Left blue bank"
    assert row["binding"] == {"channel": "0"}, (
        "what it was bound to is kept, so re-binding it later is recognisably the "
        "same hardware rather than something new"
    )


def test_unbinding_publishes_the_tombstone_and_audits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`adopted=False` on the device's own subject, not a deleted subject.

    A deletion simply vanishes, and a hardware-io that was offline for it comes
    back believing the device is still its to build. `factory.py` builds nothing
    for an unadopted assignment, so the tombstone is what removes the driver.
    """
    RecordingPublisher.published = []
    monkeypatch.setattr("bellasreef_api.app.AssignmentPublisher", RecordingPublisher)

    async def scenario() -> tuple[list[str], str, str]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        audit = Audit()
        c, headers = await client_for(engine, audit=audit, nats_url="nats://127.0.0.1:4222")
        try:
            await bind_light(c, headers, "led-blue", "0")
            response = await c.delete("/api/v1/devices/led-blue", headers=headers)
            assert response.status_code == 204, response.text
            return audit.events, audit.category("device.bound"), audit.category("device.unbound")
        finally:
            await c.aclose()
            await engine.dispose()

    events, bound_category, unbound_category = run(scenario)
    # Pairing and minting a token go through the same sink; only the device
    # events are this test's business.
    assert [e for e in events if e.startswith("device.")] == ["device.bound", "device.unbound"]

    # A device change is config, not auth, and the category is what decides
    # which subject it is published on. Asserted once for this area rather than
    # on every event — the sink used to drop it, so nothing checked it at all.
    assert bound_category == "config"
    assert unbound_category == "config"

    assert [(a.device_id, a.adopted) for a in RecordingPublisher.published] == [
        ("led-blue", True),
        ("led-blue", False),
    ]
    tombstone = RecordingPublisher.published[-1]
    assert tombstone.binding == {"channel": "0"}, (
        "the retained last value then records which channel was released, not "
        "merely that something happened"
    )


def test_unbinding_an_unknown_device_is_a_404() -> None:
    async def scenario() -> tuple[int, int]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "led-blue", "0")
            missing = await c.delete("/api/v1/devices/no-such-device", headers=headers)
            await c.delete("/api/v1/devices/led-blue", headers=headers)
            again = await c.delete("/api/v1/devices/led-blue", headers=headers)
            return missing.status_code, again.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    missing, again = run(scenario)
    assert missing == 404
    assert again == 404, "already unbound is not a second success"


def test_unbinding_needs_a_bearer() -> None:
    async def scenario() -> int:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "led-blue", "0")
            return (await c.delete("/api/v1/devices/led-blue")).status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 401


# --------------------------------------------------- detached-device lifecycle


def test_device_view_reports_adopted() -> None:
    """A freshly bound device shows `adopted: true`."""

    async def scenario() -> list[dict[str, Any]]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "pi-pwm-0", "0")
            body: list[dict[str, Any]] = (await c.get("/api/v1/devices", headers=headers)).json()
            return body
        finally:
            await c.aclose()
            await engine.dispose()

    devices = run(scenario)
    assert devices, "the fixture bound at least one device"
    assert all(d["adopted"] is True for d in devices)


def test_unbound_device_is_listed_detached() -> None:
    """Unbinding no longer makes a device vanish from the list — it detaches."""

    async def scenario() -> dict[str, Any]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "pi-pwm-0", "0")
            await c.delete("/api/v1/devices/pi-pwm-0", headers=headers)
            return await get_device(c, headers, "pi-pwm-0")
        finally:
            await c.aclose()
            await engine.dispose()

    row = run(scenario)
    assert row["adopted"] is False
    assert row["channel"] is None


def test_readopt_restores_the_binding() -> None:
    async def scenario() -> tuple[int, dict[str, Any]]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "pi-pwm-0", "0")
            await c.delete("/api/v1/devices/pi-pwm-0", headers=headers)
            r = await c.post("/api/v1/devices/pi-pwm-0/readopt", headers=headers)
            return r.status_code, r.json()
        finally:
            await c.aclose()
            await engine.dispose()

    code, body = run(scenario)
    assert code == 200
    assert body["adopted"] is True
    assert body["channel"] == "0", "the same channel as before, from the remembered binding"


def test_readopt_publishes_the_assignment_and_audits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-adoption re-publishes the retained subject and audits `config`.

    Mirrors `test_unbinding_publishes_the_tombstone_and_audits_it`: a
    hardware-io that was offline for the readopt must still be able to build
    the driver from the retained last value on the device's subject.
    """
    RecordingPublisher.published = []
    monkeypatch.setattr("bellasreef_api.app.AssignmentPublisher", RecordingPublisher)

    async def scenario() -> list[str]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        audit = Audit()
        c, headers = await client_for(engine, audit=audit, nats_url="nats://127.0.0.1:4222")
        try:
            await bind_light(c, headers, "pi-pwm-0", "0")
            await c.delete("/api/v1/devices/pi-pwm-0", headers=headers)
            response = await c.post("/api/v1/devices/pi-pwm-0/readopt", headers=headers)
            assert response.status_code == 200, response.text
            return audit.events
        finally:
            await c.aclose()
            await engine.dispose()

    events = run(scenario)
    assert [e for e in events if e.startswith("device.")] == [
        "device.bound",
        "device.unbound",
        "device.bound",
    ]
    assert [(a.device_id, a.adopted) for a in RecordingPublisher.published] == [
        ("pi-pwm-0", True),
        ("pi-pwm-0", False),
        ("pi-pwm-0", True),
    ]


def test_readopt_refuses_a_device_that_was_never_bound() -> None:
    """`NOT adopted` alone is not `detached` — a registered-but-unbound probe
    has no remembered channel to reattach, and the devices CHECK constraint
    (0013) refuses `adopted = true` without both `driver_type` and `binding`
    set. `Store.readopt_device` must filter this out itself rather than let
    that constraint violation surface as a 500 from the UPDATE.
    """

    async def scenario() -> dict[str, Any] | None:
        engine = await fresh_engine()
        store = Store(engine)
        await store.upsert_sensor(
            device_id="probe-1",
            driver_id="ds18b20",
            sensor_type="temp",
            poll_interval_s=5.0,
            transport="local",
        )
        try:
            return await store.readopt_device("probe-1")
        finally:
            await engine.dispose()

    assert run(scenario) is None


def test_readopt_unknown_device_is_404() -> None:
    async def scenario() -> int:
        engine = await fresh_engine()
        c, headers = await client_for(engine)
        try:
            r = await c.post("/api/v1/devices/no-such-device/readopt", headers=headers)
            return r.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    assert run(scenario) == 404


def test_readopt_conflicts_when_channel_taken() -> None:
    """A genuine channel conflict is unreachable through this store — pinned.

    `Store.bind_device` matches an existing row on ``(driver_type, channel)``
    regardless of ``adopted`` (see its docstring, and
    `test_unbinding_frees_the_channel_to_be_bound_again` above, which already
    pins the same finding for the plain bind endpoint). So binding a "new"
    device onto pi-pwm-0's freed channel does not create a *second*, competing
    row for `readopt_device`'s holder check to catch — it resurrects and
    re-adopts pi-pwm-0's own row, discarding the proposed id. There is no
    sequence through the public API that leaves one row detached and a
    *different* row adopted on the same (driver_type, channel) pair at once.

    `ChannelHeldError` stays in `readopt_device` anyway, as a guard against a
    future change to `bind_device`'s matching rather than a path reachable
    today (task-3 brief's own NOTE anticipates exactly this).  What this test
    pins instead: the "competing" bind succeeds (200) and lands back on
    pi-pwm-0 — not on the proposed id — and a subsequent explicit readopt on
    that now-already-adopted row 404s, because it is no longer detached.
    """

    async def scenario() -> tuple[int, dict[str, Any], int]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "pi-pwm-0", "0")
            await c.delete("/api/v1/devices/pi-pwm-0", headers=headers)
            resurrect = await bind_light(c, headers, "led-red", "0")
            again = await c.post("/api/v1/devices/pi-pwm-0/readopt", headers=headers)
            return resurrect.status_code, resurrect.json(), again.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    resurrect_code, resurrect_body, readopt_code = run(scenario)
    assert resurrect_code == 200
    assert resurrect_body["device_id"] == "pi-pwm-0", "the old row, not the proposed led-red"
    assert readopt_code == 404, "already adopted (via the resurrection above), not detached"


def test_forget_deletes_only_detached() -> None:
    async def scenario() -> tuple[int, int, list[dict[str, Any]], int]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        c, headers = await client_for(engine)
        try:
            await bind_light(c, headers, "pi-pwm-0", "0")
            still_bound = await c.post("/api/v1/devices/pi-pwm-0/forget", headers=headers)
            await c.delete("/api/v1/devices/pi-pwm-0", headers=headers)
            forgotten = await c.post("/api/v1/devices/pi-pwm-0/forget", headers=headers)
            devices = (await c.get("/api/v1/devices", headers=headers)).json()
            again = await c.post("/api/v1/devices/pi-pwm-0/forget", headers=headers)
            return still_bound.status_code, forgotten.status_code, devices, again.status_code
        finally:
            await c.aclose()
            await engine.dispose()

    still_bound, forgotten, devices, again = run(scenario)
    assert still_bound == 409, "still bound"
    assert forgotten == 204
    assert all(d["device_id"] != "pi-pwm-0" for d in devices)
    assert again == 404


def test_forget_audits_and_publishes_no_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached device holds no channel claim to retract — no publish."""
    RecordingPublisher.published = []
    monkeypatch.setattr("bellasreef_api.app.AssignmentPublisher", RecordingPublisher)

    async def scenario() -> list[str]:
        engine = await fresh_engine()
        await announce(engine, "pi-pwm", "0")
        audit = Audit()
        c, headers = await client_for(engine, audit=audit, nats_url="nats://127.0.0.1:4222")
        try:
            await bind_light(c, headers, "pi-pwm-0", "0")
            await c.delete("/api/v1/devices/pi-pwm-0", headers=headers)
            response = await c.post("/api/v1/devices/pi-pwm-0/forget", headers=headers)
            assert response.status_code == 204, response.text
            return audit.events
        finally:
            await c.aclose()
            await engine.dispose()

    events = run(scenario)
    assert [e for e in events if e.startswith("device.")] == [
        "device.bound",
        "device.unbound",
        "device.forgotten",
    ]
    # Bound, then unbound: no third publish for the forget.
    assert len(RecordingPublisher.published) == 2

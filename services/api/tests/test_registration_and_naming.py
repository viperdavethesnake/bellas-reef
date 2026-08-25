# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Device registration upserts, operator naming, and self-revocation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from bellasreef_api.app import build_app
from bellasreef_api.store import Store
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

    def category(self, event: str) -> str:
        return next(c for e, _, c in self.records if e == event)

    def detail(self, event: str) -> dict[str, Any]:
        """The detail the first occurrence of ``event`` was recorded with."""
        return next(d for e, d, _ in self.records if e == event)


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE sensor_alerts, overrides, pairing_windows, pairing_requests, "
                "paired_clients, devices CASCADE"
            )
        )
    return engine


async def paired_with_id(app: Any, name: str = "phone") -> tuple[dict[str, str], str]:
    """Bearer headers for a freshly paired client, and the id the hub gave it."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub"
    ) as c:
        granted = (await c.post("/api/v1/pair", json={"client_name": name})).json()
        tok = (
            await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
        ).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}, str(granted["client_id"])


async def paired(app: Any, name: str = "phone") -> dict[str, str]:
    headers, _ = await paired_with_id(app, name)
    return headers


class TestRegistrationUpsert:
    def test_a_registration_creates_the_device_once(self) -> None:
        async def scenario() -> tuple[bool, bool, list[dict[str, Any]]]:
            engine = await fresh_engine()
            store = Store(engine)
            first = await store.upsert_sensor(
                device_id="ds18b20-28-000000bfe244",
                driver_id="ds18b20",
                sensor_type="temp",
                poll_interval_s=5.0,
                transport="local",
            )
            second = await store.upsert_sensor(
                device_id="ds18b20-28-000000bfe244",
                driver_id="ds18b20",
                sensor_type="temp",
                poll_interval_s=7.0,
                transport="local",
            )
            rows = await store.list_devices()
            await engine.dispose()
            return first, second, rows

        first, second, rows = run(scenario)
        assert first is True, "the first announcement creates the row"
        assert second is False, "a re-announcement updates rather than duplicating"
        assert len(rows) == 1
        assert rows[0]["poll_interval_s"] == 7.0, "hardware-owned fields do follow the hardware"
        assert rows[0]["channel"] is None, (
            "announced but never bound: it claims no physical channel yet"
        )

    def test_a_re_announcement_does_not_erase_operator_settings(self) -> None:
        """hardware-io restarting must not reset the name or the alert band.

        This is the whole reason the upsert lists its columns explicitly instead
        of taking EXCLUDED.*: the operator owns some of this row and the
        hardware owns the rest.
        """

        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            store = Store(engine)
            await store.upsert_sensor(
                device_id="probe",
                driver_id="ds18b20",
                sensor_type="temp",
                poll_interval_s=5.0,
                transport="local",
            )
            await store.set_display_name("probe", "Display tank")
            await store.set_thresholds("probe", minimum=24.0, maximum=27.0, clear_margin=0.5)

            await store.upsert_sensor(
                device_id="probe",
                driver_id="ds18b20",
                sensor_type="temp",
                poll_interval_s=5.0,
                transport="local",
            )
            rows = await store.list_devices()
            await engine.dispose()
            return rows[0]

        row = run(scenario)
        assert row["display_name"] == "Display tank"
        assert (row["alert_min"], row["alert_max"]) == (24.0, 27.0)


class TestNaming:
    def test_naming_a_device_and_clearing_it(self) -> None:
        async def scenario() -> tuple[int, Any, Any, Audit, str]:
            engine = await fresh_engine()
            store = Store(engine)
            await store.upsert_sensor(
                device_id="probe",
                driver_id="ds18b20",
                sensor_type="temp",
                poll_interval_s=5.0,
                transport="local",
            )
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers, client_id = await paired_with_id(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                named = await c.patch(
                    "/api/v1/devices/probe", headers=headers, json={"display_name": "Frag tank"}
                )
                cleared = await c.patch(
                    "/api/v1/devices/probe", headers=headers, json={"display_name": None}
                )
            await engine.dispose()
            return (
                named.status_code,
                named.json()["display_name"],
                cleared.json()["display_name"],
                audit,
                client_id,
            )

        code, named, cleared, audit, client_id = run(scenario)
        assert code == 200
        assert named == "Frag tank"
        assert cleared is None, "clearing the name goes back to the raw id, not a blank label"
        assert "device.renamed" in audit.events
        # Named by whom: the sink fills ``actor`` with ``api`` unless the detail
        # says otherwise, and a rename attributed to the process is no
        # attribution at all. ``actor`` is the client's resolved name — a bare
        # UUID names nobody (rehearsal 2026-08-24, checkpoint D) — and
        # ``actor_id`` keeps the durable identity.
        assert audit.detail("device.renamed") == {
            "device_id": "probe",
            "display_name": "Frag tank",
            "actor": "phone",
            "actor_id": client_id,
        }

    def test_a_whitespace_name_normalises_to_no_name(self) -> None:
        """Otherwise every client renders a blank label where the id used to be."""

        async def scenario() -> Any:
            engine = await fresh_engine()
            store = Store(engine)
            await store.upsert_sensor(
                device_id="probe",
                driver_id="ds18b20",
                sensor_type="temp",
                poll_interval_s=5.0,
                transport="local",
            )
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.patch(
                    "/api/v1/devices/probe", headers=headers, json={"display_name": "   "}
                )
            await engine.dispose()
            return r.json()["display_name"]

        assert run(scenario) is None

    def test_renaming_an_unknown_device_is_404(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.patch(
                    "/api/v1/devices/nope", headers=headers, json={"display_name": "x"}
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 404


class TestApproversAndSelfRevoke:
    def test_a_fresh_hub_has_no_approvers_and_open_pairing(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                body: dict[str, Any] = (await c.get("/api/v1/info")).json()
            await engine.dispose()
            return body

        info = run(scenario)
        assert info["pairing_open"] is True
        assert info["approvers_available"] is False

    def test_a_paired_hub_reports_an_approver(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                body: dict[str, Any] = (await c.get("/api/v1/info")).json()
            await engine.dispose()
            return body

        info = run(scenario)
        assert info["pairing_open"] is False
        assert info["approvers_available"] is True

    def test_signing_out_the_last_client_leaves_nobody_to_approve(self) -> None:
        """The lockout this whole change exists to make visible.

        Before `approvers_available`, this state reported `pairing_open: false`
        and nothing else, so a client said "an already-paired device will need to
        approve this one" to somebody who had no such device and no way to get
        one except the recovery CLI.
        """

        async def scenario() -> tuple[int, dict[str, Any]]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                gone = await c.delete("/api/v1/clients/me", headers=headers)
                body: dict[str, Any] = (await c.get("/api/v1/info")).json()
            await engine.dispose()
            return gone.status_code, body

        code, info = run(scenario)
        assert code == 200
        assert info["pairing_open"] is False, "TOFU-ever stays shut; revoking cannot reopen it"
        assert info["approvers_available"] is False, "and the app can now say so"

    def test_a_revoked_client_cannot_keep_using_its_token(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                await c.delete("/api/v1/clients/me", headers=headers)
                after = await c.get("/api/v1/clients", headers=headers)
            await engine.dispose()
            return after.status_code

        assert run(scenario) == 401

    def test_self_revoke_needs_authentication(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.delete("/api/v1/clients/me")
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 401

    def test_me_is_not_parsed_as_a_client_id(self) -> None:
        """Route ordering guard.

        `/clients/{client_id}` is a UUID path parameter. Declared first, it would
        swallow `/clients/me` and 422 before the handler ever ran.
        """

        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.delete("/api/v1/clients/me", headers=headers)
            await engine.dispose()
            return r.status_code

        assert run(scenario) != 422


class TestObserveOnlyClosesTheCommandPath:
    """docs/device-classes.md §2.3, enforced at the API boundary."""

    async def _seed(self, engine: AsyncEngine, authority: str) -> None:
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
                    "device_id": f"light-{authority}",
                    "authority": authority,
                    "failsafe": authority == "authoritative",
                    "transport": "local" if authority == "authoritative" else "network",
                    "safe": '{"kind": "pwm", "duty": 0.0}'
                    if authority == "authoritative"
                    else None,
                    "runtime": 3600.0 if authority == "authoritative" else None,
                    "beat": 15.0 if authority == "authoritative" else None,
                },
            )

    def test_an_override_on_an_observe_only_device_is_refused(self) -> None:
        """Refused *here*, not filtered downstream.

        Filtering later would mean the command was accepted, journaled, and
        quietly dropped by something that happened to know better — while the
        operator was told the hold had been placed.
        """

        async def scenario() -> tuple[int, str]:
            engine = await fresh_engine()
            await self._seed(engine, "observe_only")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={"target": "light-observe_only", "duty": 0.5, "duration_s": 600},
                )
            await engine.dispose()
            return r.status_code, r.text

        code, body = run(scenario)
        assert code == 409
        assert "observe_only" in body

    def test_an_authoritative_device_still_accepts_overrides(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            await self._seed(engine, "authoritative")
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={"target": "light-authoritative", "duty": 0.5, "duration_s": 600},
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 200

    def test_an_unknown_target_is_not_treated_as_observe_only(self) -> None:
        """An absent row means "the hub does not know this device", which is a
        different thing from "this device refuses commands"."""

        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={"target": "not-a-device", "duty": 0.5, "duration_s": 600},
                )
            await engine.dispose()
            return r.status_code

        assert run(scenario) != 409

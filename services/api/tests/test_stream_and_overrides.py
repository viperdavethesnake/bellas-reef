# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Sensor/device listing, override endpoints, and the WebSocket bridge."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from bellasreef_api.app import build_app
from bellasreef_api.stream import parse_auth_frame
from fastapi.testclient import TestClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"
_NATS = "BELLASREEF_TEST_NATS_URL"

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


async def fresh_engine() -> AsyncEngine:
    # NullPool: asyncpg binds a connection to the loop that opened it, and
    # the WebSocket tests hand this engine to an app running on TestClient's
    # own loop. Pooling would reuse a connection across loops and fail with a
    # "future attached to a different loop" that looks nothing like its cause.
    engine = create_async_engine(os.environ[_PG], future=True, poolclass=NullPool)
    async with engine.begin() as conn:
        # CASCADE, not DELETE: dosing_journal and calibration_records hold
        # RESTRICT foreign keys onto devices, so a plain delete trips over rows
        # an earlier test left behind.
        await conn.execute(
            text(
                "TRUNCATE overrides, pairing_windows, pairing_requests, "
                "paired_clients, devices CASCADE"
            )
        )
    return engine


async def seed_devices(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
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


async def paired(engine: AsyncEngine, app: Any) -> dict[str, str]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub"
    ) as c:
        granted = (await c.post("/api/v1/pair", json={"client_name": "phone"})).json()
        tok = (
            await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
        ).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


class TestHardwareListing:
    def test_devices_lists_sensors_and_actuators(self) -> None:
        async def scenario() -> list[dict[str, Any]]:
            engine = await fresh_engine()
            await seed_devices(engine)
            app = build_app(engine, audit=Audit())
            headers = await paired(engine, app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                body: list[dict[str, Any]] = (
                    await c.get("/api/v1/devices", headers=headers)
                ).json()
            await engine.dispose()
            return body

        devices = run(scenario)
        assert {d["device_id"] for d in devices} == {"display-tank", "led-blue"}
        # The role added in contracts 2.0.0 is what lets a client render a
        # light differently from a doser.
        assert next(d for d in devices if d["device_id"] == "led-blue")["role"] == "light"
        # seed_devices inserts directly, bypassing bind_device, so neither row
        # is adopted or carries a binding — channel must read as None for both.
        assert all(d["channel"] is None for d in devices)

    def test_sensors_filters_to_sensors(self) -> None:
        async def scenario() -> list[dict[str, Any]]:
            engine = await fresh_engine()
            await seed_devices(engine)
            app = build_app(engine, audit=Audit())
            headers = await paired(engine, app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                body: list[dict[str, Any]] = (
                    await c.get("/api/v1/sensors", headers=headers)
                ).json()
            await engine.dispose()
            return body

        sensors = run(scenario)
        assert [s["device_id"] for s in sensors] == ["display-tank"]
        assert sensors[0]["sensor_type"] == "temp"

    def test_hardware_endpoints_require_auth(self) -> None:
        async def scenario() -> tuple[int, int]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                codes = (
                    (await c.get("/api/v1/devices")).status_code,
                    (await c.get("/api/v1/sensors")).status_code,
                )
            await engine.dispose()
            return codes

        assert run(scenario) == (401, 401)


class TestOverrideEndpoints:
    def test_create_list_and_release(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            await seed_devices(engine)
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(engine, app)
            out: dict[str, Any] = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={
                        "target": "led-blue",
                        "duty": 0.0,
                        "duration_s": 1800,
                        "reason": "feed",
                    },
                )
                out["create_code"] = created.status_code
                out["created"] = created.json()

                listed = await c.get("/api/v1/overrides", headers=headers)
                out["listed"] = listed.json()

                released = await c.delete(
                    f"/api/v1/overrides/{out['created']['id']}", headers=headers
                )
                out["release_code"] = released.status_code
                out["after"] = (await c.get("/api/v1/overrides", headers=headers)).json()
            await engine.dispose()
            out["events"] = audit.events
            return out

        out = run(scenario)
        assert out["create_code"] == 200
        assert out["created"]["target"] == "led-blue"
        assert out["created"]["expires_in_s"] == pytest.approx(1800, abs=2)
        assert len(out["listed"]) == 1
        assert out["release_code"] == 200
        assert out["after"] == []
        assert "override.created" in out["events"]
        assert "override.released" in out["events"]

    def test_releasing_twice_is_a_404(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(engine, app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                created = (
                    await c.post(
                        "/api/v1/overrides",
                        headers=headers,
                        json={"target": "led-blue", "duty": 0.0, "duration_s": 60},
                    )
                ).json()
                await c.delete(f"/api/v1/overrides/{created['id']}", headers=headers)
                again = await c.delete(f"/api/v1/overrides/{created['id']}", headers=headers)
            await engine.dispose()
            return again.status_code

        assert run(scenario) == 404

    def test_a_superseding_hold_audits_the_ending_of_the_one_it_displaces(self) -> None:
        """E1 (UX review 2026-08-17): the log showed three "started" and no
        "ended" for one light re-held twice, because supersede — the store's
        own release reason — was never audited. The API knows exactly what
        it displaced (``PlacedOverride.superseded``, captured atomically with
        the insert) and records the ending beside the new beginning, in the
        same event a manual release writes, so the trail reads as one."""

        async def scenario() -> tuple[list[tuple[str, dict[str, Any], str]], str, str]:
            engine = await fresh_engine()
            await seed_devices(engine)
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(engine, app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                first = (
                    await c.post(
                        "/api/v1/overrides",
                        headers=headers,
                        json={"target": "led-blue", "duty": 0.0, "duration_s": 600},
                    )
                ).json()
                second = (
                    await c.post(
                        "/api/v1/overrides",
                        headers=headers,
                        json={"target": "led-blue", "duty": 0.5, "duration_s": 600},
                    )
                ).json()
            await engine.dispose()
            return audit.records, first["id"], second["id"]

        records, first_id, second_id = run(scenario)
        events = [(e, c) for e, _, c in records if e.startswith("override.")]
        assert events == [
            ("override.created", "command"),
            ("override.released", "command"),
            ("override.created", "command"),
        ], "the ending of the first hold is recorded before the second begins"
        released = next(d for e, d, _ in records if e == "override.released")
        assert released["override_id"] == first_id
        assert released["target"] == "led-blue"
        assert released["reason"] == "superseded"
        assert released["superseded_by"] == second_id

    def test_an_untrusted_clock_returns_503_not_a_broken_override(self) -> None:
        """A deadline from a clock about to be stepped is not the duration the
        operator asked for, so the API refuses rather than storing a wrong one."""

        async def scenario() -> tuple[int, str]:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            headers = await paired(engine, app)

            # A second app with the gate shut, as a pre-sync boot would have it.
            # Injected rather than monkeypatched: the predicate is a constructor
            # argument precisely so this is expressible without reaching into
            # another module's globals.
            gated = build_app(engine, audit=Audit(), clock_trusted=lambda: False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gated), base_url="http://hub"
            ) as c:
                r = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={"target": "led-blue", "duty": 0.0, "duration_s": 60},
                )
                body = r.text
            await engine.dispose()
            return r.status_code, body

        code, body = run(scenario)
        assert code == 503
        assert "not synchronised" in body

    def test_override_endpoints_require_auth(self) -> None:
        async def scenario() -> int:
            engine = await fresh_engine()
            app = build_app(engine, audit=Audit())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.get("/api/v1/overrides")
            await engine.dispose()
            return r.status_code

        assert run(scenario) == 401

    def test_transition_defaults_to_ramp_and_snap_round_trips(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            await seed_devices(engine)
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(engine, app)
            out: dict[str, Any] = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                omitted = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={"target": "led-blue", "duty": 0.5, "duration_s": 60},
                )
                out["omitted"] = omitted.json()
                snapped = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={
                        "target": "led-blue",
                        "duty": 0.0,
                        "duration_s": 60,
                        "transition": "snap",
                    },
                )
                out["snapped"] = snapped.json()
                out["listed"] = (await c.get("/api/v1/overrides", headers=headers)).json()
                bad = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={
                        "target": "led-blue",
                        "duty": 0.0,
                        "duration_s": 60,
                        "transition": "fade",
                    },
                )
                out["bad_code"] = bad.status_code
            await engine.dispose()
            out["created_details"] = [d for e, d, _ in audit.records if e == "override.created"]
            return out

        out = run(scenario)
        assert out["omitted"]["transition"] == "ramp"
        assert out["snapped"]["transition"] == "snap"
        assert [o["transition"] for o in out["listed"]] == ["snap"]  # superseded the ramp one
        assert out["bad_code"] == 422
        assert [d["transition"] for d in out["created_details"]] == ["ramp", "snap"]


class TestAuthFrameParsing:
    def test_a_valid_frame_yields_the_token(self) -> None:
        assert parse_auth_frame(json.dumps({"token": "abc"})) == "abc"

    @pytest.mark.parametrize(
        "raw", ["not json", "[]", '{"nope": 1}', '{"token": ""}', '{"token": 5}']
    )
    def test_malformed_frames_yield_nothing(self, raw: str) -> None:
        assert parse_auth_frame(raw) is None


@pytest.mark.skipif(not os.environ.get(_NATS), reason=f"{_NATS} not set")
class TestWebSocketStream:
    """Auth-by-first-message, and override context on state frames."""

    def _app_and_token(self) -> tuple[Any, str, AsyncEngine]:
        async def setup() -> tuple[Any, str, AsyncEngine]:
            engine = await fresh_engine()
            await seed_devices(engine)
            app = build_app(engine, audit=Audit(), nats_url=os.environ[_NATS])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                granted = (await c.post("/api/v1/pair", json={"client_name": "phone"})).json()
                tok = (
                    await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
                ).json()
            return app, tok["access_token"], engine

        return run(setup)

    def test_a_socket_without_a_valid_token_is_closed(self) -> None:
        app, _, engine = self._app_and_token()
        with TestClient(app) as client, pytest.raises(Exception):  # noqa: B017
            with client.websocket_connect("/api/v1/stream") as ws:
                ws.send_text(json.dumps({"token": "garbage"}))
                ws.receive_text()
        run(engine.dispose)

    def test_an_authenticated_socket_gets_ready_then_live_frames(self) -> None:
        """The bridge forwards state, enriched with override context.

        The enrichment is the "loudly visible" requirement: a client showing a
        channel at 0% must be able to say whether that is the schedule or a
        hold, and when the hold ends.
        """
        app, token, engine = self._app_and_token()

        async def place_override_and_publish() -> None:
            from bellasreef_contracts import ActuatorState, BinaryLevel
            from bellasreef_db import OverrideStore
            from bellasreef_hardware_io.spine import Spine

            await OverrideStore(engine).create(
                "led-blue", 0.0, 1800, reason="feed", transition="snap"
            )
            spine = Spine(os.environ[_NATS])
            await spine.connect()
            await asyncio.sleep(0.4)  # let the bridge's subscription settle
            await spine.publish_state(
                ActuatorState(
                    message_id=uuid.uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="hardware-io",
                    actuator_id="led-blue",
                    level=BinaryLevel(on=False),
                    reason="commanded",
                    since=datetime.now(UTC),
                )
            )
            await spine.close()

        with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as ws:
            ws.send_text(json.dumps({"token": token}))
            ready = json.loads(ws.receive_text())
            assert ready["kind"] == "ready"

            # From a thread with its own loop: TestClient drives the app on an
            # anyio portal, and starting a second asyncio.run on this thread
            # would put the publisher's connection on a loop the portal is
            # already using.
            publisher = threading.Thread(target=lambda: asyncio.run(place_override_and_publish()))
            publisher.start()
            publisher.join(timeout=30)

            # Since H3 the bridge replays every actuator's retained state right
            # after `ready` — including other suites' actuators on this shared
            # BR_STATE — so read until led-blue's frame rather than assuming
            # the first frame is the live one.
            frame: dict[str, Any] = {}
            for _ in range(64):
                frame = json.loads(ws.receive_text())
                if frame["kind"] == "state" and frame["payload"]["actuator_id"] == "led-blue":
                    break
            assert frame["kind"] == "state"
            assert frame["payload"]["actuator_id"] == "led-blue"
            assert frame["override"] is not None, "state frames must carry override context"
            assert frame["override"]["duty"] == pytest.approx(0.0)
            assert frame["override"]["expires_in_s"] > 0
            assert frame["override"]["transition"] == "snap"

        run(engine.dispose)

    def test_a_fresh_socket_receives_the_last_known_state_before_anything_changes(self) -> None:
        """H3 (2026-08-18): a client that connected after the last state
        publish saw "no state yet" on every light, indefinitely — the bridge
        forwarded live messages only, and BR_STATE's retained last value per
        actuator was never read.

        Publish led-blue first, connect second. Then publish a *sentinel* live
        state for another actuator: everything the socket receives before the
        sentinel is replay, so led-blue must be among it. Bounded by the
        sentinel rather than a timeout — TestClient's receive has none, and a
        test that hangs on RED tells nobody anything.
        """
        app, token, engine = self._app_and_token()
        sentinel = f"sentinel-{uuid.uuid4().hex[:8]}"

        async def publish(actuator_id: str, duty: float) -> None:
            from bellasreef_contracts import ActuatorState, PwmLevel
            from bellasreef_hardware_io.spine import Spine

            spine = Spine(os.environ[_NATS])
            await spine.connect()
            await spine.publish_state(
                ActuatorState(
                    message_id=uuid.uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="hardware-io",
                    actuator_id=actuator_id,
                    level=PwmLevel(duty=duty),
                    reason="commanded",
                    since=datetime.now(UTC),
                )
            )
            await spine.close()

        def publish_in_thread(actuator_id: str, duty: float) -> None:
            t = threading.Thread(target=lambda: asyncio.run(publish(actuator_id, duty)))
            t.start()
            t.join(timeout=30)

        publish_in_thread("led-blue", 0.42)

        with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as ws:
            ws.send_text(json.dumps({"token": token}))
            ready = json.loads(ws.receive_text())
            assert ready["kind"] == "ready"

            time.sleep(0.4)  # let the bridge's live subscription settle
            publish_in_thread(sentinel, 0.99)

            before_sentinel: list[dict[str, Any]] = []
            for _ in range(64):
                frame = json.loads(ws.receive_text())
                if frame["kind"] == "state" and frame["payload"]["actuator_id"] == sentinel:
                    break
                before_sentinel.append(frame)
            else:
                pytest.fail(f"sentinel never arrived; saw {before_sentinel}")

            mine = [
                f
                for f in before_sentinel
                if f["kind"] == "state" and f["payload"]["actuator_id"] == "led-blue"
            ]
            assert mine, "led-blue's retained state must arrive before the first live frame"
            assert mine[0]["payload"]["level"]["duty"] == pytest.approx(0.42)

        run(engine.dispose)

    def test_a_revoked_client_is_disconnected_at_the_next_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revocation reaches an open socket, not just the next handshake.

        Found live 2026-08-13: a revoked device's Tank tab kept rendering
        telemetry because the only is_active check ran at the handshake.
        The recheck is time-gated at STREAM_REVOKE_RECHECK_S; zero here so
        the very next frame carries the check.
        """
        import bellasreef_api.app as app_module

        monkeypatch.setattr(app_module, "STREAM_REVOKE_RECHECK_S", 0.0)
        app, token, engine = self._app_and_token()

        async def revoke_self_and_publish() -> None:
            from bellasreef_contracts import ActuatorState, BinaryLevel
            from bellasreef_hardware_io.spine import Spine

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.delete(
                    "/api/v1/clients/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200, response.text

            spine = Spine(os.environ[_NATS])
            await spine.connect()
            await asyncio.sleep(0.4)  # let the bridge's subscription settle
            await spine.publish_state(
                ActuatorState(
                    message_id=uuid.uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="hardware-io",
                    actuator_id="led-blue",
                    level=BinaryLevel(on=False),
                    reason="commanded",
                    since=datetime.now(UTC),
                )
            )
            await spine.close()

        with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as ws:
            ws.send_text(json.dumps({"token": token}))
            ready = json.loads(ws.receive_text())
            assert ready["kind"] == "ready"

            worker = threading.Thread(target=lambda: asyncio.run(revoke_self_and_publish()))
            worker.start()
            worker.join(timeout=30)

            # The frame that would have been sent instead carries the close.
            # Since H3 the socket may already hold replayed retained states,
            # buffered before the revoke; drain those — each is a real frame —
            # and the close must come within a bounded number of reads.
            with pytest.raises(Exception):  # noqa: B017
                for _ in range(64):
                    frame = json.loads(ws.receive_text())
                    assert frame["kind"] in {"state", "sensor", "alert"}, frame

        run(engine.dispose)

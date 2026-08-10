# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The full pairing and token lifecycle, against a real Postgres.

Needs `BELLASREEF_TEST_DATABASE_URL`. The constraints being relied on here —
the TOFU window keying on rows-ever, the revoked-iff-hash-cleared invariant —
are database behaviour, and a mocked store would prove none of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from bellasreef_api.app import build_app
from bellasreef_api.security import issue_access_token
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class Audit:
    """Captures auth events so the audit requirement is assertable."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: str, detail: dict[str, Any]) -> None:
        self.events.append((event, detail))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE pairing_requests, paired_clients CASCADE"))
    return engine


class Harness:
    def __init__(self, engine: AsyncEngine, audit: Audit, **kw: Any) -> None:
        self.engine = engine
        self.audit = audit
        self.app = build_app(engine, audit=audit, **kw)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://hub")


async def harness(**kw: Any) -> Harness:
    return Harness(await fresh_engine(), Audit(), **kw)


async def bearer(c: httpx.AsyncClient, refresh: str) -> dict[str, str]:
    r = await c.post("/api/v1/token", json={"refresh_token": refresh})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# --------------------------------------------------------------------- TOFU


def test_the_tofu_window_grants_the_first_client_and_then_closes() -> None:
    """The window closes on first success — not on a timer, not on a count of
    live clients. Revoking everything must not reopen it."""

    async def scenario() -> tuple[int, int, list[str]]:
        h = await harness()
        async with h.client() as c:
            info = (await c.get("/api/v1/info")).json()
            assert info["pairing_open"] is True
            assert info["paired_client_count"] == 0

            first = await c.post("/api/v1/pair", json={"client_name": "David's iPhone"})
            assert first.status_code == 200, first.text
            assert first.json()["refresh_token"]

            # The window is shut the moment the first client exists.
            assert (await c.get("/api/v1/info")).json()["pairing_open"] is False

            second = await c.post("/api/v1/pair", json={"client_name": "second"})
        await h.engine.dispose()
        return first.status_code, second.status_code, h.audit.names()

    first_code, second_code, events = run(scenario)
    assert first_code == 200
    assert second_code == 202, "the TOFU window must be closed for the second client"
    assert "pair.tofu_granted" in events
    assert "pair.requested" in events


def test_revoking_every_client_does_not_reopen_the_tofu_window() -> None:
    """The security hole this guards: if the window keyed on *live* clients,
    an attacker who revoked everything would get open pairing back."""

    async def scenario() -> tuple[bool, int]:
        h = await harness()
        async with h.client() as c:
            granted = (await c.post("/api/v1/pair", json={"client_name": "only"})).json()
            headers = await bearer(c, granted["refresh_token"])

            revoked = await c.delete(f"/api/v1/clients/{granted['client_id']}", headers=headers)
            assert revoked.status_code == 200, revoked.text

            info = (await c.get("/api/v1/info")).json()
            retry = await c.post("/api/v1/pair", json={"client_name": "attacker"})
        await h.engine.dispose()
        return info["pairing_open"], retry.status_code

    pairing_open, retry_code = run(scenario)
    assert pairing_open is False, "window reopened after revoking every client"
    # 403, not 202: with every client revoked there is nobody left to approve,
    # so the honest answer is "run `bellasreef pair`" rather than a request that
    # would sit pending forever.
    assert retry_code == 403, "pairing must not be granted outright after revocation"


# ------------------------------------------------------------ approval path


def test_the_202_poll_approve_path() -> None:
    """Second device: 202, poll while pending, approve from the first, collect."""

    async def scenario() -> dict[str, Any]:
        h = await harness()
        out: dict[str, Any] = {}
        async with h.client() as c:
            first = (await c.post("/api/v1/pair", json={"client_name": "iPhone"})).json()
            headers = await bearer(c, first["refresh_token"])

            pending = await c.post("/api/v1/pair", json={"client_name": "iPad"})
            out["pending_code"] = pending.status_code
            request_id = pending.json()["request_id"]

            poll1 = await c.get(f"/api/v1/pair/{request_id}")
            out["poll_pending_code"] = poll1.status_code

            approve = await c.post(f"/api/v1/pair/{request_id}/approve", headers=headers)
            out["approve_code"] = approve.status_code

            poll2 = await c.get(f"/api/v1/pair/{request_id}")
            out["poll_granted_code"] = poll2.status_code
            out["granted"] = poll2.json()

            # One approval, one credential. A second collection must fail.
            poll3 = await c.get(f"/api/v1/pair/{request_id}")
            out["poll_again_code"] = poll3.status_code

            clients = (await c.get("/api/v1/clients", headers=headers)).json()
            out["client_count"] = len(clients)
        await h.engine.dispose()
        out["events"] = h.audit.names()
        return out

    out = run(scenario)
    assert out["pending_code"] == 202
    assert out["poll_pending_code"] == 202
    assert out["approve_code"] == 200
    assert out["poll_granted_code"] == 200
    assert out["granted"]["refresh_token"]
    assert out["poll_again_code"] == 410, "an approval must yield exactly one credential"
    assert out["client_count"] == 2
    assert "pair.approved" in out["events"]
    assert "pair.collected" in out["events"]


def test_approval_requires_an_authenticated_client() -> None:
    """Approve-from-paired: an unauthenticated caller cannot self-approve."""

    async def scenario() -> int:
        h = await harness()
        async with h.client() as c:
            await c.post("/api/v1/pair", json={"client_name": "first"})
            pending = await c.post("/api/v1/pair", json={"client_name": "intruder"})
            request_id = pending.json()["request_id"]
            r = await c.post(f"/api/v1/pair/{request_id}/approve")
        await h.engine.dispose()
        return r.status_code

    assert run(scenario) == 401


def test_denied_pairing_is_refused_at_the_poll() -> None:
    async def scenario() -> tuple[int, list[str]]:
        h = await harness()
        async with h.client() as c:
            first = (await c.post("/api/v1/pair", json={"client_name": "first"})).json()
            headers = await bearer(c, first["refresh_token"])
            request_id = (await c.post("/api/v1/pair", json={"client_name": "nope"})).json()[
                "request_id"
            ]
            await c.post(f"/api/v1/pair/{request_id}/deny", headers=headers)
            poll = await c.get(f"/api/v1/pair/{request_id}")
        await h.engine.dispose()
        return poll.status_code, h.audit.names()

    code, events = run(scenario)
    assert code == 403
    assert "pair.denied" in events


def test_an_expired_pairing_request_is_gone() -> None:
    """Expiry is evaluated against the stored timestamp, so a request ages out
    without a sweeper having run."""

    async def scenario() -> int:
        h = await harness()
        async with h.client() as c:
            await c.post("/api/v1/pair", json={"client_name": "first"})
            request_id = (await c.post("/api/v1/pair", json={"client_name": "slow"})).json()[
                "request_id"
            ]
            async with h.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE pairing_requests SET expires_at = :past WHERE id = :id"),
                    {"past": datetime.now(UTC) - timedelta(seconds=1), "id": UUID(request_id)},
                )
            poll = await c.get(f"/api/v1/pair/{request_id}")
        await h.engine.dispose()
        return poll.status_code

    assert run(scenario) == 410


# ------------------------------------------------------------------ revocation


def test_revocation_kills_refresh_but_not_an_outstanding_jwt() -> None:
    """The asymmetry auth.md §3 specifies, asserted in both directions.

    Revoking deletes the refresh hash, so no NEW access tokens can be minted.
    Outstanding JWTs remain cryptographically valid until exp — there is no
    denylist. What stops a revoked client acting is the liveness check on
    stateful endpoints, so the exposure is bounded by the token TTL for
    signature-only purposes and is immediate for anything that matters.
    """

    async def scenario() -> dict[str, Any]:
        h = await harness()
        out: dict[str, Any] = {}
        async with h.client() as c:
            a = (await c.post("/api/v1/pair", json={"client_name": "keeper"})).json()
            keeper = await bearer(c, a["refresh_token"])

            req = (await c.post("/api/v1/pair", json={"client_name": "lost"})).json()
            await c.post(f"/api/v1/pair/{req['request_id']}/approve", headers=keeper)
            lost = (await c.get(f"/api/v1/pair/{req['request_id']}")).json()

            lost_headers = await bearer(c, lost["refresh_token"])
            out["before_revoke"] = (
                await c.get("/api/v1/clients", headers=lost_headers)
            ).status_code

            await c.delete(f"/api/v1/clients/{lost['client_id']}", headers=keeper)

            # 1. The refresh token is dead: no new JWTs.
            out["refresh_after_revoke"] = (
                await c.post("/api/v1/token", json={"refresh_token": lost["refresh_token"]})
            ).status_code

            # 2. The already-issued JWT still verifies cryptographically...
            from bellasreef_api.security import verify_access_token

            raw = lost_headers["Authorization"][7:]
            out["jwt_still_verifies"] = (
                str(verify_access_token(raw, await h.app.state.signing_secret_getter()))
                == lost["client_id"]
            )

            # 3. ...but is refused on a stateful endpoint by the liveness check.
            out["stateful_after_revoke"] = (
                await c.get("/api/v1/clients", headers=lost_headers)
            ).status_code

            out["keeper_unaffected"] = (await c.get("/api/v1/clients", headers=keeper)).status_code
        await h.engine.dispose()
        out["events"] = h.audit.names()
        return out

    out = run(scenario)
    assert out["before_revoke"] == 200
    assert out["refresh_after_revoke"] == 401, "revocation must kill the refresh token"
    assert out["jwt_still_verifies"] is True, (
        "outstanding JWTs are not recalled — there is no denylist, by design"
    )
    assert out["stateful_after_revoke"] == 401
    assert out["keeper_unaffected"] == 200
    assert "client.revoked" in out["events"]
    assert "token.rejected" in out["events"]


def test_a_revoked_client_cannot_revoke_anything() -> None:
    """Its JWT still verifies, but the liveness check refuses it."""

    async def scenario() -> int:
        h = await harness()
        async with h.client() as c:
            a = (await c.post("/api/v1/pair", json={"client_name": "solo"})).json()
            headers = await bearer(c, a["refresh_token"])
            await c.delete(f"/api/v1/clients/{a['client_id']}", headers=headers)
            # Same token: still cryptographically valid, but the client is gone.
            second = await c.delete(f"/api/v1/clients/{a['client_id']}", headers=headers)
        await h.engine.dispose()
        return second.status_code

    assert run(scenario) == 401  # the revoked caller cannot act at all


# --------------------------------------------------------------------- tokens


def test_an_unknown_refresh_token_is_refused() -> None:
    async def scenario() -> int:
        h = await harness()
        async with h.client() as c:
            r = await c.post("/api/v1/token", json={"refresh_token": "not-a-real-token"})
        await h.engine.dispose()
        return r.status_code

    assert run(scenario) == 401


def test_an_expired_jwt_is_refused() -> None:
    async def scenario() -> int:
        h = await harness()
        async with h.client() as c:
            a = (await c.post("/api/v1/pair", json={"client_name": "solo"})).json()
            expired, _ = issue_access_token(
                UUID(a["client_id"]),
                await h.app.state.signing_secret_getter(),
                ttl_s=-10,
            )
            r = await c.get("/api/v1/clients", headers={"Authorization": f"Bearer {expired}"})
        await h.engine.dispose()
        return r.status_code

    assert run(scenario) == 401


def test_a_token_signed_with_the_wrong_key_is_refused() -> None:
    async def scenario() -> int:
        h = await harness()
        async with h.client() as c:
            a = (await c.post("/api/v1/pair", json={"client_name": "solo"})).json()
            forged, _ = issue_access_token(UUID(a["client_id"]), "attacker-key")
            r = await c.get("/api/v1/clients", headers={"Authorization": f"Bearer {forged}"})
        await h.engine.dispose()
        return r.status_code

    assert run(scenario) == 401


# ------------------------------------------------------------------ unauth set


def test_only_info_and_healthz_are_unauthenticated() -> None:
    """auth.md §2: everything else needs a bearer token."""

    async def scenario() -> dict[str, int]:
        h = await harness()
        async with h.client() as c:
            await c.post("/api/v1/pair", json={"client_name": "first"})
            return {
                "healthz": (await c.get("/healthz")).status_code,
                "info": (await c.get("/api/v1/info")).status_code,
                "clients": (await c.get("/api/v1/clients")).status_code,
            }

    codes = run(scenario)
    assert codes["healthz"] == 200
    assert codes["info"] == 200
    assert codes["clients"] == 401


def test_info_leaks_nothing_sensitive() -> None:
    async def scenario() -> dict[str, Any]:
        h = await harness()
        async with h.client() as c:
            await c.post("/api/v1/pair", json={"client_name": "David's iPhone"})
            body: dict[str, Any] = (await c.get("/api/v1/info")).json()
        await h.engine.dispose()
        return body

    body = run(scenario)
    assert set(body) == {
        "name",
        "api_version",
        "contracts_version",
        "paired_client_count",
        "pairing_open",
    }
    # A count, never the names — /info is shown before any commitment.
    assert "David's iPhone" not in str(body)


def test_the_openapi_schema_is_generated() -> None:
    """The spec is the product surface; it must render."""

    async def scenario() -> dict[str, Any]:
        h = await harness()
        async with h.client() as c:
            spec: dict[str, Any] = (await c.get("/openapi.json")).json()
        await h.engine.dispose()
        return spec

    spec = run(scenario)
    paths = set(spec["paths"])
    assert {"/healthz", "/api/v1/info", "/api/v1/pair", "/api/v1/token", "/api/v1/clients"} <= paths
    # Not this pass.
    assert "/api/v1/stream" not in paths


# ------------------------------------------------------- audit reaches the spine

_NATS = "BELLASREEF_TEST_NATS_URL"


@pytest.mark.skipif(not os.environ.get(_NATS), reason=f"{_NATS} not set")
def test_auth_events_reach_bellasreef_audit_auth() -> None:
    """auth.md §3, end to end on a real broker.

    Closes the gap flagged at the end of the previous pass: the sink existed
    but was wired to a no-op, so auth events were logged locally and never
    reached the trail.
    """

    async def scenario() -> tuple[int, list[str]]:
        from bellasreef_api.audit import NatsAuditSink
        from bellasreef_contracts import subjects
        from bellasreef_hardware_io.spine import Spine

        spine = Spine(os.environ[_NATS])
        await spine.connect()
        await spine.provision()
        await spine.js.purge_stream("BR_AUDIT")
        for consumer in await spine.js.consumers_info("BR_AUDIT"):
            await spine.js.delete_consumer("BR_AUDIT", consumer.name)

        sub = await spine.js.pull_subscribe(
            subjects.ALL_AUDIT, durable=f"auth-{uuid.uuid4().hex[:8]}", stream="BR_AUDIT"
        )

        sink = NatsAuditSink(os.environ[_NATS])
        engine = await fresh_engine()
        app = build_app(engine, audit=sink)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            granted = (await c.post("/api/v1/pair", json={"client_name": "phone"})).json()
            await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})

        msgs = await sub.fetch(10, timeout=5.0)
        subjects_seen = [m.subject for m in msgs]
        events = []
        for m in msgs:
            events.append(json.loads(m.data)["event"])
            await m.ack()

        await sink.close()
        await engine.dispose()
        await spine.close()
        return len(msgs), events + subjects_seen

    count, seen = run(scenario)
    assert count >= 2, "pairing and token minting should both be audited"
    assert "pair.tofu_granted" in seen
    assert "token.minted" in seen
    assert all(s == "bellasreef.audit.auth" for s in seen if s.startswith("bellasreef"))


@pytest.mark.skipif(not os.environ.get(_NATS), reason=f"{_NATS} not set")
def test_a_broker_outage_does_not_break_pairing() -> None:
    """The deliberate trade: a logging failure must not lock an operator out."""

    async def scenario() -> int:
        from bellasreef_api.audit import NatsAuditSink

        sink = NatsAuditSink("nats://127.0.0.1:1")  # nothing listening
        engine = await fresh_engine()
        app = build_app(engine, audit=sink)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            r = await c.post("/api/v1/pair", json={"client_name": "phone"})
        await engine.dispose()
        return r.status_code

    assert run(scenario) == 200


# ------------------------------------------------------------ recovery window


async def _wipe_windows(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE pairing_windows CASCADE"))


def test_all_clients_revoked_and_no_window_is_a_403_not_an_endless_poll() -> None:
    """Nobody can approve, so say so.

    Returning 202 here would leave the app polling a request that no existing
    client will ever see, which looks like a hang rather than a recoverable
    state.
    """

    async def scenario() -> tuple[int, list[str]]:
        h = await harness()
        await _wipe_windows(h.engine)
        async with h.client() as c:
            granted = (await c.post("/api/v1/pair", json={"client_name": "only"})).json()
            headers = await bearer(c, granted["refresh_token"])
            await c.delete(f"/api/v1/clients/{granted['client_id']}", headers=headers)

            blocked = await c.post("/api/v1/pair", json={"client_name": "new phone"})
        await h.engine.dispose()
        return blocked.status_code, h.audit.names()

    code, events = run(scenario)
    assert code == 403
    assert "pair.no_approver" in events


def test_the_recovery_window_lets_exactly_one_client_in() -> None:
    """The fire escape, end to end — and it is spent by the first user."""

    async def scenario() -> dict[str, Any]:
        h = await harness()
        await _wipe_windows(h.engine)
        out: dict[str, Any] = {}
        async with h.client() as c:
            granted = (await c.post("/api/v1/pair", json={"client_name": "only"})).json()
            headers = await bearer(c, granted["refresh_token"])
            await c.delete(f"/api/v1/clients/{granted['client_id']}", headers=headers)

            out["locked_out"] = (
                await c.post("/api/v1/pair", json={"client_name": "phone"})
            ).status_code

            # SSH to the hub and run `bellasreef pair`.
            from bellasreef_api.store import Store

            await Store(h.engine).open_pairing_window("david@hub", 300)

            recovered = await c.post("/api/v1/pair", json={"client_name": "phone"})
            out["recovered"] = recovered.status_code
            out["got_token"] = bool(recovered.json().get("refresh_token"))

            # The window is spent. A second client must NOT get an immediate
            # grant; it falls through to the normal approval path, which is now
            # available again because recovery produced a live approver.
            out["second"] = (
                await c.post("/api/v1/pair", json={"client_name": "gatecrasher"})
            ).status_code
        await h.engine.dispose()
        out["events"] = h.audit.names()
        return out

    out = run(scenario)
    assert out["locked_out"] == 403
    assert out["recovered"] == 200
    assert out["got_token"] is True
    assert out["second"] == 202, (
        "a window is one credential: the next client must go through approval, "
        "not ride the spent window in"
    )
    assert "pair.window_used" in out["events"]


def test_an_expired_window_does_not_let_anyone_in() -> None:
    async def scenario() -> int:
        h = await harness()
        await _wipe_windows(h.engine)
        async with h.client() as c:
            granted = (await c.post("/api/v1/pair", json={"client_name": "only"})).json()
            headers = await bearer(c, granted["refresh_token"])
            await c.delete(f"/api/v1/clients/{granted['client_id']}", headers=headers)

            from bellasreef_api.store import Store

            store = Store(h.engine)
            await store.open_pairing_window(
                "david@hub", 300, now=datetime.now(UTC) - timedelta(hours=1)
            )

            r = await c.post("/api/v1/pair", json={"client_name": "late"})
        await h.engine.dispose()
        return r.status_code

    assert run(scenario) == 403


def test_recovery_does_not_reopen_the_tofu_window() -> None:
    """The property the window design exists to preserve.

    After recovery, `pairing_open` must still be False: the TOFU-ever window is
    keyed on client rows having existed, and the recovery path adds a client
    rather than deleting history.
    """

    async def scenario() -> tuple[bool, int]:
        h = await harness()
        await _wipe_windows(h.engine)
        async with h.client() as c:
            granted = (await c.post("/api/v1/pair", json={"client_name": "only"})).json()
            headers = await bearer(c, granted["refresh_token"])
            await c.delete(f"/api/v1/clients/{granted['client_id']}", headers=headers)

            from bellasreef_api.store import Store

            await Store(h.engine).open_pairing_window("david@hub", 300)
            await c.post("/api/v1/pair", json={"client_name": "recovered"})

            info = (await c.get("/api/v1/info")).json()
        await h.engine.dispose()
        return info["pairing_open"], info["paired_client_count"]

    pairing_open, count = run(scenario)
    assert pairing_open is False, "recovery must not reopen open pairing"
    assert count >= 2


def test_a_window_is_not_needed_while_someone_can_still_approve() -> None:
    """Normal operation is unchanged: 202 and an approval, not a window."""

    async def scenario() -> int:
        h = await harness()
        await _wipe_windows(h.engine)
        async with h.client() as c:
            await c.post("/api/v1/pair", json={"client_name": "first"})
            r = await c.post("/api/v1/pair", json={"client_name": "second"})
        await h.engine.dispose()
        return r.status_code

    assert run(scenario) == 202

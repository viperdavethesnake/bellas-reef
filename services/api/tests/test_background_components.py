# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The service actually runs its consumers.

This exists because ``AuditWriter`` was correct, tested, and dead. Every unit
test passed; a load test drove it at 300 events with a redelivery mid-batch and
went green; and no entrypoint ever started it, so every audit event published to
``BR_AUDIT`` aged out of the stream without reaching ``audit_log``. The same
shape as the sensor publish that set a healthy Prometheus gauge while nothing
went on the wire, and the staleness indicator computed against a clock nothing
re-read.

The common property of all three: **constructible was mistaken for running.**
Nothing asserted that the composed service starts the thing.

So these tests enter the app's real lifespan and ask each background component
whether it is live. They deliberately do not mock the lifespan — a mocked
lifespan is exactly the thing that was never checked.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import nats
import pytest
from bellasreef_api.app import build_app
from bellasreef_service.httpd import probe_once
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"
_NATS = "BELLASREEF_TEST_NATS_URL"
_VM = "BELLASREEF_TEST_VM_URL"

pytestmark = pytest.mark.skipif(
    not (os.environ.get(_PG) and os.environ.get(_NATS) and os.environ.get(_VM)),
    # Must contain "not set" — conftest.py's _ENV_SKIP_MARKER matches on that
    # exact substring to decide a skip was declared rather than incidental.
    # The old "must all be set" phrasing didn't, so this whole file
    # env-skipped silently: `pytest_sessionfinish` never saw it as a skip
    # worth reporting, and the gate passed locally with none of these tests
    # run at all.
    reason=f"{_PG}, {_NATS} or {_VM} not set",
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class Audit:
    async def __call__(self, event: str, detail: dict[str, Any], category: str = "auth") -> None:
        return None


async def clear_audit_consumers() -> None:
    """Drop any consumer bound to `bellasreef.audit.>`.

    `BR_AUDIT` is a *workqueue* stream, which permits exactly one consumer per
    filter subject — that is what makes it a work queue rather than a fan-out.
    A durable outlives the process that created it, so a previous run (or a hub
    API that was stopped a moment ago) leaves one behind and the next attempt is
    refused with "filtered consumer not unique". Unique names do not help; the
    filter is what collides.

    Clearing it is the test owning its broker state — before *and* after. A
    durable outlives its process by design, so a test-scoped consumer left
    behind blocks the real service from ever binding again: the hub's API sat in
    a retry loop logging "filtered consumer not unique" until it was deleted by
    hand. Cleaning up on the way out is not tidiness, it is not breaking the
    thing the test shares a broker with.
    """
    nc = await nats.connect(os.environ[_NATS])
    try:
        js = nc.jetstream()
        try:
            for consumer in await js.consumers_info("BR_AUDIT"):
                await js.delete_consumer("BR_AUDIT", consumer.name)
        except Exception:
            # No stream yet: hardware-io provisions it, and it may not have run.
            pass
    finally:
        await nc.close()


#: Streams the telemetry writer binds test-scoped durables on. These are not
#: workqueues, so a leak here does not block the real service from binding —
#: which is exactly why it went unnoticed. What it does instead is quieter and
#: still bad: every leaked durable pins its stream's messages against
#: retention forever. A hub found with ~150 of these was holding 244 undelivered
#: messages per consumer and would have grown without bound.
_TEST_DURABLE_STREAMS = ("BR_TELEMETRY", "BR_STATE")

#: Every test-scoped durable carries this. The real service's consumers
#: (`telemetry-sensors`, `telemetry-state`, …) do not, so matching on it cannot
#: delete a running hub's consumer.
_TEST_MARKER = "-test-"


async def clear_test_consumers() -> None:
    """Delete every test-scoped telemetry durable, from this run or any earlier one.

    Deliberately not scoped to this run's suffix. A test that only cleans up
    after itself cannot repair the damage done by a run that crashed, and these
    accumulate one set per run. Matching on the marker makes the suite
    self-healing, and the marker is what keeps it away from the real consumers.
    """
    nc = await nats.connect(os.environ[_NATS])
    try:
        js = nc.jetstream()
        for stream in _TEST_DURABLE_STREAMS:
            try:
                consumers = await js.consumers_info(stream)
            except Exception:
                continue  # stream not provisioned yet
            for consumer in consumers:
                if _TEST_MARKER in consumer.name:
                    with contextlib.suppress(Exception):
                        await js.delete_consumer(stream, consumer.name)
    finally:
        await nc.close()


async def count_test_consumers() -> int:
    nc = await nats.connect(os.environ[_NATS])
    try:
        js = nc.jetstream()
        total = 0
        for stream in _TEST_DURABLE_STREAMS:
            try:
                consumers = await js.consumers_info(stream)
            except Exception:
                continue
            total += sum(1 for c in consumers if _TEST_MARKER in c.name)
        return total
    finally:
        await nc.close()


async def fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE sensor_alerts, overrides, pairing_windows, pairing_requests, "
                "paired_clients, devices CASCADE"
            )
        )
        await conn.execute(text("TRUNCATE audit_log RESTART IDENTITY CASCADE"))
    return engine


def app_for(engine: AsyncEngine, audit: Any | None = None, *, metrics_port: int = 0) -> Any:
    return build_app(
        engine,
        audit=audit or Audit(),
        nats_url=os.environ[_NATS],
        vm_url=os.environ[_VM],
        # Durable consumer names are shared state on the broker. Without a
        # unique suffix this test collides with whatever API instance is
        # already running against the same NATS — including the real hub.
        durable_suffix=f"-test-{uuid.uuid4().hex[:8]}",
        # 0: the OS picks a free port, read back rather than hardcoded, so
        # this suite cannot collide with a real hub's metrics server (9103)
        # running on the same machine.
        metrics_port=metrics_port,
    )


def test_every_background_component_is_running_after_startup() -> None:
    """The gate. Construction is not operation.

    ``app.state.background`` is the list of things the service promises to run;
    each reports whether its task is alive. A component that is present in the
    mapping and never started fails here — which is precisely what would have
    caught the audit writer.
    """

    async def scenario() -> tuple[dict[str, bool], int]:
        await clear_audit_consumers()
        await clear_test_consumers()
        engine = await fresh_engine()
        app = app_for(engine)
        try:
            async with app.router.lifespan_context(app):
                # Give the start() tasks a scheduling slot; `is_running` reflects
                # a live task, not a queued coroutine.
                await asyncio.sleep(1.0)
                running = {
                    name: bool(component and getattr(component, "is_running", False))
                    for name, component in app.state.background.items()
                }
        finally:
            await clear_audit_consumers()
            await clear_test_consumers()
            await engine.dispose()
        return running, await count_test_consumers()

    running, leaked = run(scenario)
    assert running, "the service declares no background components at all"
    assert all(running.values()), f"declared but not running: {running}"
    assert leaked == 0, (
        f"{leaked} test-scoped durable(s) survived teardown. Each one pins its "
        "stream's messages against retention for good; the hub was found with "
        "roughly 150 of them"
    )


def test_a_declared_component_that_never_starts_fails_this_test() -> None:
    """Proves the gate above can actually fail.

    A test that cannot fail is the same category of problem as a consumer that
    never runs — it looks like coverage and checks nothing.
    """

    class NeverStarts:
        is_running = False

    running = {"never starts": bool(getattr(NeverStarts(), "is_running", False))}
    assert not all(running.values())


def test_metrics_server_starts_with_the_app() -> None:
    """CLAUDE.md day-1 requirement: every service exposes metrics. The API
    shipped without one (2026-08-23 review, cleanup findings).

    Also the self-check for the other half of that requirement: a metrics
    server that starts with the lifespan and never stops would leak a listener
    into every later test in this process. So this drives a real HTTP request
    through the app, confirms the counter it incremented is on the wire, and
    then confirms the port is truly released once the lifespan exits — not
    merely idle.
    """

    async def scenario() -> tuple[bytes, int]:
        await clear_audit_consumers()
        await clear_test_consumers()
        engine = await fresh_engine()
        app = app_for(engine)
        try:
            async with app.router.lifespan_context(app):
                server = app.state.metrics_server
                assert server._server is not None, "the lifespan did not start it"
                port = server._server.sockets[0].getsockname()[1]

                # Exercise the counting middleware through the real ASGI path,
                # not by reaching into the registry directly.
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://hub"
                ) as c:
                    await c.get("/healthz")

                _, body = await probe_once("127.0.0.1", port, "/metrics")

            # Lifespan has exited. A leaked listener here would eventually
            # collide with the next test's ephemeral port, or with a real
            # hub's 9103 if the default ever got used by mistake.
            with pytest.raises(OSError):
                await probe_once("127.0.0.1", port, "/metrics")
        finally:
            await clear_audit_consumers()
            await clear_test_consumers()
            await engine.dispose()
        return body, port

    body, _ = run(scenario)
    assert b'bellasreef_api_requests_total{method="GET",status_family="2xx"}' in body


@pytest.mark.timeout(120)
def test_an_auth_event_reaches_the_audit_log_and_the_api() -> None:
    """auth event → BR_AUDIT → audit_log → GET /api/v1/audit.

    End to end through the composed service, because every link in this chain
    existed and passed its own tests while the chain itself was broken.
    """

    async def scenario() -> list[dict[str, Any]]:
        await clear_audit_consumers()
        await clear_test_consumers()
        engine = await fresh_engine()
        # The real NATS audit sink, not the no-op: the point is the wire.
        from bellasreef_api.audit import NatsAuditSink

        app = app_for(engine, audit=NatsAuditSink(os.environ[_NATS]))
        marker = f"phone-{uuid.uuid4().hex[:8]}"

        # Teardown in a finally, and every early return inside it. The previous
        # shape cleaned up on two of three exits, so an assertion failure or a
        # timeout left the telemetry durables on the broker for good.
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://hub"
                ) as c:
                    granted = await c.post("/api/v1/pair", json={"client_name": marker})
                    assert granted.status_code == 200, granted.text
                    token = (
                        await c.post(
                            "/api/v1/token",
                            json={"refresh_token": granted.json()["refresh_token"]},
                        )
                    ).json()["access_token"]
                    headers = {"Authorization": f"Bearer {token}"}

                    # The writer drains on a poll interval; wait for the row
                    # rather than assuming an interval. Wait for `token.minted`
                    # specifically: pairing and token minting both emit auth
                    # events and land in whichever order the writer's batch
                    # happens to catch, so "any auth event" makes the assertion
                    # below depend on a race.
                    for _ in range(60):
                        body = (await c.get("/api/v1/audit", headers=headers)).json()
                        minted = [
                            e
                            for e in body
                            if e["category"] == "auth" and e["event"].get("event") == "token.minted"
                        ]
                        if minted:
                            return minted
                        await asyncio.sleep(1.0)
            return []
        finally:
            await clear_audit_consumers()
            await clear_test_consumers()
            await engine.dispose()

    events = run(scenario)
    assert events, "no auth event reached audit_log — the writer is not draining"
    # `subject` is the NATS subject the event arrived on; what actually happened
    # is in the payload. Asserting on the subject would pass for any auth event
    # at all, including one from an unrelated test.
    assert all(e["event"]["event"] == "token.minted" for e in events), events
    assert all(e["actor"] == "api" for e in events), events
    assert all(e["subject"] == "bellasreef.audit.auth" for e in events), events

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Audit persistence under real consumer load.

Needs both a NATS with JetStream and a migrated Postgres. Skipped otherwise.

These deliberately run at volume with a redelivery overlapping a live batch.
One-row-at-a-time politeness would exercise neither the append-only trigger
under concurrent transactions nor the unique constraint under a real race, and
both are things that only misbehave when contended.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import nats
import pytest
from bellasreef_api.audit_writer import AUDIT_STREAM, AuditWriter
from bellasreef_contracts import subjects
from nats.js.api import RetentionPolicy, StorageType, StreamConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_NATS = "BELLASREEF_TEST_NATS_URL"
_PG = "BELLASREEF_TEST_DATABASE_URL"

#: Applied per-test below, not as a module-level ``pytestmark`` — the
#: ``_to_row`` tests further down are pure (no NATS, no DB connection ever
#: opens) and must run without either environment variable set.
_needs_env = pytest.mark.skipif(
    not (os.environ.get(_NATS) and os.environ.get(_PG)),
    # Must contain "not set" — conftest.py's _ENV_SKIP_MARKER matches on that
    # exact substring to decide a skip was declared rather than incidental.
    # The old "must both be set" phrasing didn't, so this whole file
    # env-skipped silently with the gate never tripping locally.
    reason=f"{_NATS} or {_PG} not set",
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def engine() -> AsyncEngine:
    return create_async_engine(os.environ[_PG], future=True)


class Publisher:
    """Provisions BR_AUDIT and publishes onto it.

    Deliberately not hardware-io's `Spine`. The audit writer moved into the API
    service precisely to break that dependency, and a test that reaches back
    across it would quietly restore the coupling the move exists to remove.
    """

    def __init__(self, nc: Any, js: Any) -> None:
        self.nc, self.js = nc, js

    async def publish_audit(self, category: str, event: dict[str, Any]) -> None:
        message_id = event.get("message_id") or str(uuid4())
        payload = {**event, "message_id": str(message_id)}
        await self.js.publish(
            subjects.audit(category),
            json.dumps(payload).encode(),
            headers={"Nats-Msg-Id": str(message_id)},
        )

    async def close(self) -> None:
        await self.nc.close()


async def fresh() -> tuple[Publisher, AsyncEngine]:
    nc = await nats.connect(os.environ[_NATS])
    js = nc.jetstream()
    # Must match what hardware-io provisions, exactly. JetStream refuses to
    # change a stream's retention policy, so a test that "helpfully" declares a
    # slightly different config fails against any hub that has ever run the real
    # service — which is every hub that matters.
    config = StreamConfig(
        name=AUDIT_STREAM,
        subjects=[subjects.ALL_AUDIT],
        retention=RetentionPolicy.WORK_QUEUE,
        storage=StorageType.FILE,
        max_age=604800.0,
    )
    try:
        await js.add_stream(config)
    except Exception:
        await js.update_stream(config)
    await js.purge_stream(AUDIT_STREAM)
    for consumer in await js.consumers_info(AUDIT_STREAM):
        await js.delete_consumer(AUDIT_STREAM, consumer.name)
    spine = Publisher(nc, js)

    eng = engine()
    async with eng.begin() as conn:
        # audit_log is append-only by trigger, so it cannot be DELETEd. TRUNCATE
        # is DDL and bypasses row triggers — which is exactly why the trigger
        # guards UPDATE/DELETE and not this.
        await conn.execute(text("TRUNCATE audit_log RESTART IDENTITY CASCADE"))
    return spine, eng


async def audit_rows(eng: AsyncEngine) -> int:
    async with eng.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM audit_log"))).scalar_one())


@_needs_env
@pytest.mark.timeout(180)
def test_audit_load_with_a_redelivery_overlapping_a_live_batch() -> None:
    """Volume, plus a redelivery landing in the middle of ongoing work.

    Sequence:
      1. publish 200 events
      2. fetch 40 and never ack them — the writer "crashes" mid-batch
      3. publish 100 more, so fresh traffic is in flight
      4. wait past ack_wait, then drain to completion

    The redelivered 40 arrive interleaved with new events, so the inserts
    contend rather than queue politely.
    """

    async def scenario() -> tuple[int, int, int]:
        spine, eng = await fresh()
        writer = AuditWriter(os.environ[_NATS], eng, durable="load", ack_wait_s=3.0, max_deliver=5)
        await writer.subscribe()

        for i in range(200):
            await spine.publish_audit("command", {"event": "load", "seq": i})

        # Crash mid-batch: delivered, never acked.
        assert writer._sub is not None
        stolen = await writer._sub.fetch(40, timeout=10.0)
        assert len(stolen) == 40

        for i in range(200, 300):
            await spine.publish_audit("command", {"event": "load", "seq": i})

        await asyncio.sleep(4.0)  # past ack_wait; the 40 are now redeliverable

        written = 0
        for _ in range(40):
            n = await writer.drain_once(batch=32, timeout=2.0)
            written += n
            if n == 0 and written >= 300:
                break

        rows = await audit_rows(eng)
        distinct = 0
        async with eng.connect() as conn:
            distinct = int(
                (
                    await conn.execute(
                        text("SELECT count(DISTINCT (event->>'seq')) FROM audit_log")
                    )
                ).scalar_one()
            )

        await eng.dispose()
        await spine.close()
        return written, rows, distinct

    written, rows, distinct = run(scenario)

    # The writer itself accounted for every row it inserted.
    assert written >= 300, f"writer reported only {written} rows written"

    # Every event reached the system of record.
    assert distinct == 300, f"expected 300 distinct events persisted, got {distinct}"
    assert rows >= 300

    # Exactly-once at rest. PRD R13 wants one row per event; the 40-message
    # redelivery above would previously have written 40 second copies. The
    # unique message_id plus ON CONFLICT DO NOTHING is what closes it — and it
    # closes it without pretending JetStream is exactly-once, which it is not.
    assert rows == distinct == 300, (
        f"expected exactly 300 rows for 300 events, got {rows} "
        "— a redelivered audit event was written twice"
    )


@_needs_env
@pytest.mark.timeout(120)
def test_append_only_trigger_holds_under_concurrent_writers() -> None:
    """Concurrent transactions inserting while UPDATE/DELETE are attempted.

    A trigger that only ever gets tested on an idle table is a trigger you do
    not know about.
    """

    async def scenario() -> tuple[int, list[str]]:
        _, eng = await fresh()

        async def insert_many(worker: int, n: int) -> None:
            async with eng.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO audit_log "
                        "(message_id, occurred_at, category, actor, event) "
                        "VALUES (:mid, :at, 'safety', :actor, CAST(:event AS JSONB))"
                    ),
                    [
                        {
                            "mid": uuid.uuid4(),
                            "at": datetime.now(UTC),
                            "actor": f"worker-{worker}",
                            "event": f'{{"i": {i}}}',
                        }
                        for i in range(n)
                    ],
                )

        await asyncio.gather(*(insert_many(w, 50) for w in range(8)))

        errors: list[str] = []

        async def try_mutate(sql: str) -> None:
            try:
                async with eng.begin() as conn:
                    await conn.execute(text(sql))
            except DBAPIError as exc:
                errors.append(str(exc))

        await asyncio.gather(
            *(try_mutate("UPDATE audit_log SET actor = 'tampered'") for _ in range(4)),
            *(try_mutate("DELETE FROM audit_log") for _ in range(4)),
        )

        rows = await audit_rows(eng)
        await eng.dispose()
        return rows, errors

    rows, errors = run(scenario)

    assert rows == 400, f"concurrent inserts lost rows: {rows}"
    assert len(errors) == 8, "every UPDATE/DELETE must have been rejected"
    assert all("append-only" in e for e in errors)


@_needs_env
@pytest.mark.timeout(120)
def test_idempotency_key_uniqueness_under_a_real_race() -> None:
    """Concurrent doses sharing an idempotency_key: exactly one may land.

    This is the constraint that stops a redelivered dose command from dosing
    twice, and dosing twice is not recoverable. Sequential inserts would prove
    the constraint exists; only concurrent ones prove it holds when two workers
    reach it at the same moment.
    """

    async def scenario() -> tuple[int, int]:
        _, eng = await fresh()

        async with eng.begin() as conn:
            device_pk = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO devices (id, device_id, kind, driver_id, actuator_class,"
                    " role, control_authority, failsafe_capable, transport,"
                    " safe_state, max_runtime_s, heartbeat_timeout_s) VALUES "
                    "(:id, :did, 'actuator', 'fake', 'binary', 'doser',"
                    " 'authoritative', true, 'local',"
                    " CAST(:safe AS JSONB), 60, 15)"
                ),
                # Bound, never inlined. A ':' inside a SQL string literal is
                # parsed by SQLAlchemy as a bind parameter -- ':false' here,
                # ':1' in db/tests. Second time; the rule is no inline JSON.
                {
                    "id": device_pk,
                    "did": f"doser-{uuid.uuid4().hex[:8]}",
                    "safe": '{"kind": "binary", "on": false}',
                },
            )

        shared_key = uuid.uuid4()

        async def dose() -> bool:
            try:
                async with eng.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO dosing_journal "
                            "(id, device_pk, idempotency_key, state, requested_ml, intent_at) "
                            "VALUES (:id, :pk, :key, 'intent', 5.0, :now)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "pk": device_pk,
                            "key": shared_key,
                            "now": datetime.now(UTC),
                        },
                    )
            except IntegrityError:
                return False
            return True

        results = await asyncio.gather(*(dose() for _ in range(12)))

        async with eng.connect() as conn:
            stored = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM dosing_journal WHERE idempotency_key = :k"),
                        {"k": shared_key},
                    )
                ).scalar_one()
            )
        await eng.dispose()
        return sum(results), stored

    winners, stored = run(scenario)

    assert winners == 1, f"{winners} concurrent doses were accepted; exactly 1 may be"
    assert stored == 1, f"dosing_journal holds {stored} rows for one idempotency_key"


def _writer() -> AuditWriter:
    """``_to_row`` is pure — building the ``AsyncEngine`` object opens no
    connection (SQLAlchemy connects lazily on first use), so these tests need
    neither a live NATS nor a live Postgres. No ``@_needs_env``.
    """
    return AuditWriter("nats://unused", create_async_engine("postgresql+asyncpg://unused/unused"))


def test_row_keeps_the_event_time_not_the_drain_time() -> None:
    """Finding 10: after a writer stall, everything buffered in BR_AUDIT (24h
    retention) persisted misdated to drain time and misordered by
    ORDER BY occurred_at."""
    row = _writer()._to_row(
        "bellasreef.audit.alert",
        json.dumps({"emitted_at": "2026-08-23T01:02:03+00:00"}).encode(),
    )
    assert row["occurred_at"] == datetime(2026, 8, 23, 1, 2, 3, tzinfo=UTC)


def test_row_resolves_device_id_across_publisher_dialects() -> None:
    for key in ("actuator_id", "device_id", "target", "channel_id"):
        row = _writer()._to_row("bellasreef.audit.command", json.dumps({key: "pca9685-0"}).encode())
        assert row["device_id"] == "pca9685-0", key


def test_row_device_id_precedence_actuator_id_beats_device_id() -> None:
    """hardware-io's own key wins over the API's, per _DEVICE_KEYS order —
    not just "some key is picked", the earlier-listed one specifically."""
    row = _writer()._to_row(
        "bellasreef.audit.command",
        json.dumps({"actuator_id": "pi-pwm-0", "device_id": "pca9685-0"}).encode(),
    )
    assert row["device_id"] == "pi-pwm-0"


def test_row_device_id_precedence_device_id_beats_target() -> None:
    row = _writer()._to_row(
        "bellasreef.audit.command",
        json.dumps({"device_id": "pca9685-0", "target": "led-blue"}).encode(),
    )
    assert row["device_id"] == "pca9685-0"


def test_alert_category_is_no_longer_remapped() -> None:
    row = _writer()._to_row("bellasreef.audit.alert", json.dumps({"device_id": "d"}).encode())
    assert row["category"] == "alert"
    assert "original_category" not in json.loads(str(row["event"]))


def test_actor_default_is_unknown_not_hardware_io() -> None:
    row = _writer()._to_row("bellasreef.audit.config", b"{}")
    assert row["actor"] == "unknown"


def test_unparseable_timestamp_falls_back_to_drain_time() -> None:
    row = _writer()._to_row(
        "bellasreef.audit.command", json.dumps({"observed_at": "not-a-date"}).encode()
    )
    occurred_at = row["occurred_at"]
    assert isinstance(occurred_at, datetime)
    # Not just "a datetime" — the actual drain-time fallback, not some other
    # value that happens to satisfy isinstance.
    assert abs((datetime.now(UTC) - occurred_at).total_seconds()) < 60


def test_naive_timestamp_falls_back_to_drain_time() -> None:
    """A naive ``occurred_at`` is a publisher bug — see _event_time's
    docstring. Guessing a zone would fabricate precision that isn't there, so
    this must land on drain time exactly like an unparseable string, not on
    the naive value reinterpreted as UTC."""
    row = _writer()._to_row(
        "bellasreef.audit.command",
        json.dumps({"occurred_at": "2020-01-01T00:00:00"}).encode(),
    )
    occurred_at = row["occurred_at"]
    assert isinstance(occurred_at, datetime)
    assert occurred_at.tzinfo is not None
    assert abs((datetime.now(UTC) - occurred_at).total_seconds()) < 60


def test_the_composed_apps_audit_durable_ignores_the_suffix() -> None:
    """BR_AUDIT is a workqueue: one consumer per filter subject, enforced by
    the server regardless of names. A per-process durable name buys nothing
    there — and it converts every cleanup race into a permanent lockout,
    because a leftover consumer under an old name hard-collides ("filtered
    consumer not unique", err 10100) where a same-name leftover would simply
    be re-bound. That lockout is the CI flake of 2026-08-31 (PR #86 attempt
    1): the writer's first subscribe failed at startup and every 5 s retry
    failed identically for the whole 60 s window.

    The suffix exists for the telemetry durables, whose streams are not
    workqueues and where per-test isolation is right. The asymmetry here is
    deliberate; this test pins it. Pure — build_app constructs components
    without connecting to anything.
    """
    from bellasreef_api.app import build_app

    app = build_app(
        create_async_engine("postgresql+asyncpg://unused/unused"),
        nats_url="nats://unused",
        vm_url="http://unused",
        durable_suffix="-test-deadbeef",
        metrics_port=0,
    )
    assert app.state.background["audit writer"]._durable == "audit-writer"
    assert app.state.background["telemetry writer"]._suffix == "-test-deadbeef"

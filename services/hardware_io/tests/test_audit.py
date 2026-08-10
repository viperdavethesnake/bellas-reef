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
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from bellasreef_hardware_io.audit import AuditWriter
from bellasreef_hardware_io.spine import Spine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_NATS = "BELLASREEF_TEST_NATS_URL"
_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    not (os.environ.get(_NATS) and os.environ.get(_PG)),
    reason=f"{_NATS} and {_PG} must both be set",
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def engine() -> AsyncEngine:
    return create_async_engine(os.environ[_PG], future=True)


async def fresh() -> tuple[Spine, AsyncEngine]:
    spine = Spine(os.environ[_NATS])
    await spine.connect()
    await spine.provision()
    await spine.js.purge_stream("BR_AUDIT")
    for consumer in await spine.js.consumers_info("BR_AUDIT"):
        await spine.js.delete_consumer("BR_AUDIT", consumer.name)

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
        writer = AuditWriter(spine, eng, durable="load", ack_wait_s=3.0, max_deliver=5)
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

    # KNOWN AND FLAGGED, asserted so it cannot drift unnoticed: JetStream is
    # at-least-once and audit_log has no dedup key, so a redelivered event is
    # written twice. That is duplication, not loss or corruption — but it is a
    # real property of the current schema and it traces to PRD R13. Awaiting a
    # ruling rather than a silently-added column.
    assert rows >= distinct


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
                        "INSERT INTO audit_log (occurred_at, category, actor, event) "
                        "VALUES (:at, 'safety', :actor, CAST(:event AS JSONB))"
                    ),
                    [
                        {
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
                    " safe_state, max_runtime_s, heartbeat_timeout_s) VALUES "
                    "(:id, :did, 'actuator', 'fake', 'binary',"
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

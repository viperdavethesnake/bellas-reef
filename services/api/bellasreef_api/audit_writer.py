# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Audit persistence.

`BR_AUDIT` is a delivery buffer; Postgres `audit_log` is the system of record.
This consumer moves events from one to the other.

Lives in the API service, alongside the telemetry writer. It used to live in
hardware-io, which contradicted device-classes.md §3 — that process is
Postgres-free by design — and, more to the point, it was constructed only by
tests: no entrypoint ever started it, so every audit event published to
`BR_AUDIT` expired in the stream without reaching `audit_log`. The same shape as
the sensor publish that recorded a healthy gauge while the wire was dead.

`audit_log` is append-only by trigger — UPDATE and DELETE raise — so this can
only ever insert. That is deliberate: post-incident analysis is worthless if
the log can be edited after the incident.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Final

import nats
from bellasreef_contracts import subjects
from bellasreef_service.logging import get_logger
from nats.aio.client import Client
from nats.js.api import AckPolicy, ConsumerConfig
from nats.js.errors import NotFoundError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: hardware-io provisions this stream. Named rather than imported: the API must
#: not depend on the hardware service, and a shared constant across a process
#: boundary is exactly the coupling that put this consumer in the wrong service
#: to begin with.
AUDIT_STREAM = "BR_AUDIT"

__all__ = ["AuditWriter"]

log = get_logger(__name__)

#: ON CONFLICT DO NOTHING against the unique message_id is what turns
#: at-least-once delivery into exactly-once at rest. Dedup lives here, in the
#: terminal store, rather than in an assumption that the broker will not
#: redeliver — it will, and that is not a fault.
_INSERT = text(
    "INSERT INTO audit_log "
    "(message_id, occurred_at, category, actor, subject, device_id, event) "
    "VALUES (:message_id, :occurred_at, :category, :actor, :subject, :device_id, "
    "        CAST(:event AS JSONB)) "
    "ON CONFLICT (message_id) DO NOTHING"
)

#: audit_log.category has a CHECK constraint; anything outside this set would be
#: rejected by the database, so it is normalised here rather than discovered as
#: a failed insert on a message we then cannot retry usefully.
_VALID_CATEGORIES = frozenset(
    {"command", "config", "auth", "state", "safety", "calibration", "alert"}
)

#: Preference order for the event's own clock; see `_event_time`.
_TIMESTAMP_KEYS: Final = ("occurred_at", "emitted_at", "observed_at")

#: Preference order for the device the event names; see `_event_device_id`.
_DEVICE_KEYS: Final = ("actuator_id", "device_id", "target", "channel_id")


def _event_time(event: dict[str, Any]) -> datetime:
    """The event's own clock, when it carries one. Drain time is a fallback,
    not a truth: after a writer stall, everything buffered in BR_AUDIT (24h)
    lands at once, and stamping arrival misorders the record the audit log
    exists to keep straight."""
    for key in _TIMESTAMP_KEYS:
        raw = event.get(key)
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                continue
            # A naive timestamp is a publisher bug — guessing a zone would
            # fabricate precision that isn't there, so drain time is the
            # safer record.
            if parsed.tzinfo is None:
                continue
            return parsed
    return datetime.now(UTC)


def _event_device_id(event: dict[str, Any]) -> str | None:
    """Publishers name their subject differently: hardware-io says
    actuator_id, the API says device_id, overrides say target, schedule
    assignment says channel_id. The column answers 'which device' for all of
    them or per-device audit queries return nothing."""
    for key in _DEVICE_KEYS:
        raw = event.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


class AuditWriter:
    """Durable consumer: `bellasreef.audit.>` → `audit_log`."""

    RETRY_S = 5.0

    def __init__(
        self,
        nats_url: str,
        engine: AsyncEngine,
        *,
        durable: str = "audit-writer",
        ack_wait_s: float = 30.0,
        max_deliver: int = 5,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._nats_url = nats_url
        self._engine = engine
        self._durable = durable
        self._ack_wait_s = ack_wait_s
        self._max_deliver = max_deliver
        self._poll_interval_s = poll_interval_s
        self._nc: Client | None = None
        self._js: Any = None
        self._sub: Any = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """True once the drain loop is live.

        Exposed so a test can assert the service *runs* this, not merely that it
        can be constructed — which was true the whole time it was doing nothing.
        """
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.close()
            self._nc = None

    async def _run_forever(self) -> None:
        while True:
            try:
                if self._sub is None:
                    await self.subscribe()
                await self.drain_once()
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise
            except NotFoundError:
                log.info("audit stream not provisioned yet; waiting")
                self._sub = None
                await asyncio.sleep(self.RETRY_S)
            except Exception:
                log.exception("audit writer stalled; retrying")
                self._sub = None
                await asyncio.sleep(self.RETRY_S)

    async def subscribe(self) -> None:
        if self._nc is None or not self._nc.is_connected:
            self._nc = await nats.connect(self._nats_url)
            self._js = self._nc.jetstream()
        self._sub = await self._js.pull_subscribe(
            subjects.ALL_AUDIT,
            durable=self._durable,
            stream=AUDIT_STREAM,
            config=ConsumerConfig(
                durable_name=self._durable,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=self._ack_wait_s,
                max_deliver=self._max_deliver,
            ),
        )

    async def drain_once(self, *, batch: int = 64, timeout: float = 1.0) -> int:
        """Persist one batch. Returns how many rows were written.

        The whole batch goes in one transaction. An audit batch that half-lands
        is worse than one that does not land at all: the redelivery would write
        the first half a second time, and there is no dedup key to notice.
        """
        if self._sub is None:
            raise RuntimeError("not subscribed")
        try:
            msgs = await self._sub.fetch(batch, timeout=timeout)
        except (TimeoutError, nats.errors.TimeoutError):
            return 0
        if not msgs:
            return 0

        rows = []
        for msg in msgs:
            rows.append(self._to_row(msg.subject, msg.data))

        async with self._engine.begin() as conn:
            await conn.execute(_INSERT, rows)

        for msg in msgs:
            await msg.ack()
        return len(rows)

    def _to_row(self, subject: str, data: bytes) -> dict[str, object]:
        try:
            event: dict[str, Any] = json.loads(data)
        except json.JSONDecodeError:
            event = {"raw": data.decode("utf-8", "replace")}

        category = subject.split(".")[-1]
        if category not in _VALID_CATEGORIES:
            # Every category a publisher legitimately uses is in
            # _VALID_CATEGORIES (alert included, as of 0021), so reaching
            # here means the subject's last token is not a category at all —
            # a malformed or future subject. 'safety' is a parking category
            # for that, not a claim that a safety event occurred: after 0021,
            # a stored category='safety' row means "unrecognised subject
            # token, original in original_category," never "a safety event."
            event = {**event, "original_category": category}
            category = "safety"

        # An event published before message_id stamping, or by something that
        # is not our publisher, still has to land. A synthesised id makes it
        # storable; it just cannot be deduplicated.
        message_id = event.get("message_id") or str(uuid.uuid4())

        return {
            "message_id": message_id,
            "occurred_at": _event_time(event),
            "category": category,
            # No publisher stamped an actor here before; defaulting to
            # "hardware-io" attributed every unstamped row to a service that
            # never published it. "unknown" is honest about what we don't know.
            "actor": str(event.get("actor", "unknown")),
            "subject": subject,
            "device_id": _event_device_id(event),
            "event": json.dumps(event),
        }

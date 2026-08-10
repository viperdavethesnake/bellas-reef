# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Audit persistence.

`BR_AUDIT` is a delivery buffer; Postgres `audit_log` is the system of record.
This consumer moves events from one to the other.

`audit_log` is append-only by trigger — UPDATE and DELETE raise — so this can
only ever insert. That is deliberate: post-incident analysis is worthless if
the log can be edited after the incident.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import nats
from bellasreef_contracts import subjects
from nats.js.api import AckPolicy, ConsumerConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bellasreef_hardware_io.logging import get_logger
from bellasreef_hardware_io.spine import AUDIT_STREAM, Spine

__all__ = ["AuditWriter"]

log = get_logger(__name__)

_INSERT = text(
    "INSERT INTO audit_log (occurred_at, category, actor, subject, device_id, event) "
    "VALUES (:occurred_at, :category, :actor, :subject, :device_id, CAST(:event AS JSONB))"
)

#: audit_log.category has a CHECK constraint; anything outside this set would be
#: rejected by the database, so it is normalised here rather than discovered as
#: a failed insert on a message we then cannot retry usefully.
_VALID_CATEGORIES = frozenset({"command", "config", "auth", "state", "safety", "calibration"})


class AuditWriter:
    """Durable consumer: `bellasreef.audit.>` → `audit_log`."""

    def __init__(
        self,
        spine: Spine,
        engine: AsyncEngine,
        *,
        durable: str = "audit-writer",
        ack_wait_s: float = 30.0,
        max_deliver: int = 5,
    ) -> None:
        self._spine = spine
        self._engine = engine
        self._durable = durable
        self._ack_wait_s = ack_wait_s
        self._max_deliver = max_deliver
        self._sub: Any = None

    async def subscribe(self) -> None:
        self._sub = await self._spine.js.pull_subscribe(
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
            event = {**event, "original_category": category}
            category = "safety"

        return {
            "occurred_at": datetime.now(UTC),
            "category": category,
            "actor": str(event.get("actor", "hardware-io")),
            "subject": subject,
            "device_id": event.get("actuator_id"),
            "event": json.dumps(event),
        }

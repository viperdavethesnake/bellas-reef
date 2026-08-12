# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Auth events onto the audit spine.

auth.md §3: pair open/close, pair success/deny, token mint and revocation all
publish to ``bellasreef.audit.auth``. That subject already works end to end —
`auth` is in the audit_log category CHECK and the AuditWriter derives it from
the trailing token — so this is the last mile, not new plumbing.

Each event is stamped with a ``message_id`` in both the payload and the
``Nats-Msg-Id`` header, matching what hardware-io does, so the writer's
``ON CONFLICT DO NOTHING`` gives exactly-once at rest against JetStream's
at-least-once delivery.

**Publishing failure does not fail the request.** That is a deliberate trade
and worth stating: an auth event that misses the trail is bad, but refusing to
let an operator pair their phone because the broker is down is worse — they
would be locked out of their own tank by a logging problem. Failures are logged
at CRITICAL so they are visible rather than silent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import nats
from bellasreef_contracts import subjects
from bellasreef_service import get_logger
from nats.aio.client import Client

__all__ = ["NatsAuditSink"]

log = get_logger(__name__)

AUDIT_CATEGORY = "auth"


class NatsAuditSink:
    """Publishes auth events to ``bellasreef.audit.auth``.

    Connects lazily on first use, for the same reason the signing key resolves
    lazily: the app must behave identically under uvicorn, under an ASGI test
    transport that never runs lifespan, and in a worker that starts late.
    """

    def __init__(self, url: str, *, source: str = "api") -> None:
        self._url = url
        self._source = source
        self._nc: Client | None = None
        #: Events this sink was asked to publish and could not. The API ignores
        #: it — a request must not fail on a logging problem — but the CLI reads
        #: it, because there the operator is standing at the terminal and is the
        #: only one who will ever see that the trail has a hole in it. A
        #: CRITICAL line in a log nobody is tailing is not telling anyone.
        self.failures = 0

    async def _client(self) -> Client | None:
        if self._nc is None or not self._nc.is_connected:
            try:
                self._nc = await nats.connect(self._url)
            except Exception:
                log.critical(
                    "audit sink cannot reach the spine; auth events are NOT being recorded",
                    extra={"url": self._url},
                    exc_info=True,
                )
                return None
        return self._nc

    async def __call__(self, event: str, detail: dict[str, Any]) -> None:
        client = await self._client()
        if client is None:
            self.failures += 1
            log.critical("auth event dropped", extra={"event": event, **detail})
            return

        message_id = str(uuid4())
        payload = {
            "message_id": message_id,
            "event": event,
            "actor": self._source,
            "occurred_at": datetime.now(UTC).isoformat(),
            **detail,
        }
        try:
            js = client.jetstream()
            await js.publish(
                subjects.audit(AUDIT_CATEGORY),
                json.dumps(payload).encode(),
                headers={"Nats-Msg-Id": message_id},
            )
        except Exception:
            self.failures += 1
            log.critical("auth event failed to publish", extra={"event": event}, exc_info=True)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None

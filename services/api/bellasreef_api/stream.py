# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""WebSocket bridge from the NATS spine to clients.

Forwards `bellasreef.state.>` and `bellasreef.sensor.>`. The API stays
stateless — it subscribes, translates, and forwards; it holds no state of its
own and makes no control decisions.

**Authentication is the first message, not a header or a query parameter.**
Browsers cannot set headers on a WebSocket handshake, and a token in the query
string ends up in access logs and proxy history. So the socket opens
unauthenticated, the client sends `{"token": "..."}`, and nothing is forwarded
until that is accepted. A socket that does not authenticate within a few
seconds is closed.

**State frames carry override context.** The time-and-scheduling contract
requires override state to be loudly visible in every client, and hardware-io
does not know about overrides — they live in Postgres and the control engine.
So the bridge enriches state frames here rather than putting override fields on
the wire contract, which would be a MAJOR bump for information the hardware
does not have.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import nats
from bellasreef_contracts import subjects
from bellasreef_db import OverrideStore
from bellasreef_service import get_logger
from nats.aio.client import Client
from nats.aio.msg import Msg

__all__ = ["AUTH_TIMEOUT_S", "StreamBridge"]

log = get_logger(__name__)

#: How long a socket may sit unauthenticated before it is closed.
AUTH_TIMEOUT_S = 10.0

#: Close codes. 1008 is "policy violation" in RFC 6455, which is the honest
#: code for "you did not authenticate" — 1000 would imply a normal close.
CLOSE_UNAUTHENTICATED = 1008


class StreamBridge:
    """One NATS connection, fanned out to every connected socket."""

    def __init__(self, nats_url: str, overrides: OverrideStore | None = None) -> None:
        self._url = nats_url
        self._overrides = overrides
        self._nc: Client | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        async with self._lock:
            if self._nc is not None and self._nc.is_connected:
                return
            self._nc = await nats.connect(self._url)
            await self._nc.subscribe(subjects.ALL_STATE, cb=self._on_message)
            await self._nc.subscribe(subjects.ALL_SENSORS, cb=self._on_message)
            log.info("stream bridge subscribed", extra={"url": self._url})

    async def _on_message(self, msg: Msg) -> None:
        try:
            payload: dict[str, Any] = json.loads(msg.data)
        except json.JSONDecodeError:
            log.warning("undecodable frame dropped", extra={"subject": msg.subject})
            return

        kind = "state" if msg.subject.startswith(f"{subjects.ROOT}.state.") else "sensor"
        frame: dict[str, Any] = {
            "kind": kind,
            "subject": msg.subject,
            "received_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }

        if kind == "state":
            frame["override"] = await self._override_for(payload.get("actuator_id"))

        encoded = json.dumps(frame)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(encoded)
            except asyncio.QueueFull:
                # A client too slow to keep up is dropped rather than allowed to
                # back-pressure the bridge onto every other client.
                log.warning("dropping a frame for a slow subscriber")

    async def _override_for(self, actuator_id: str | None) -> dict[str, Any] | None:
        """Active override on this actuator, if any.

        This is the "loudly visible" requirement: a client showing a channel at
        0% must be able to say *why* — schedule or hold — and when the hold
        ends.
        """
        if actuator_id is None or self._overrides is None:
            return None
        active = await self._overrides.active_for(actuator_id)
        if active is None:
            return None
        return {
            "id": str(active.id),
            "duty": active.duty,
            "expires_at": active.expires_at.isoformat(),
            "expires_in_s": max(
                0.0, round((active.expires_at - datetime.now(UTC)).total_seconds(), 1)
            ),
        }

    async def subscribe(self) -> asyncio.Queue[str]:
        await self._ensure_connected()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None


def parse_auth_frame(raw: str) -> str | None:
    """Pull a token out of the client's first message."""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None
    token = message.get("token") if isinstance(message, dict) else None
    return token if isinstance(token, str) and token else None


def _uuid_or_none(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None

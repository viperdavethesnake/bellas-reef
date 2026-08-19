# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""WebSocket bridge from the NATS spine to clients.

Forwards `bellasreef.state.>` and `bellasreef.sensor.>`. The API stays
stateless — it subscribes, translates, and forwards; it holds no state of its
own and makes no control decisions.

**A new socket is told where things stand before it is told what changes.**
Actuator state is published on change, not on a cadence, so a client that
connects after the last change would otherwise see nothing until the next one
— on 2026-08-18 that was every light reading "no state yet" for as long as
nobody touched a light. `BR_STATE` retains the last value per actuator, so on
each subscribe the bridge reads that once (an ephemeral consumer, deleted after)
and hands those frames over ahead of the live fan-out. The state of record is
still the stream, not this process.

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
from uuid import UUID

import nats
from bellasreef_contracts import ActuatorState, SensorAlert, SensorReading, subjects
from bellasreef_db import OverrideStore
from bellasreef_service import get_logger
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js.api import ConsumerConfig, DeliverPolicy
from pydantic import ValidationError

from bellasreef_api.frames import AlertFrame, OverrideContext, SensorFrame, StateFrame

__all__ = ["AUTH_TIMEOUT_S", "StreamBridge"]

log = get_logger(__name__)

#: How long a socket may sit unauthenticated before it is closed.
AUTH_TIMEOUT_S = 10.0

#: Close codes. 1008 is "policy violation" in RFC 6455, which is the honest
#: code for "you did not authenticate" — 1000 would imply a normal close.
CLOSE_UNAUTHENTICATED = 1008

#: The stream hardware-io provisions for `bellasreef.state.>`, retained
#: last-value-per-subject (`max_msgs_per_subject=1`). Named here rather than
#: imported from hardware-io: the API must not depend on that package.
STATE_STREAM = "BR_STATE"
#: How long one replay fetch may wait for the server. Replay is a courtesy to
#: the connecting client; a slow broker must not hold the socket hostage.
REPLAY_FETCH_TIMEOUT_S = 1.0


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
            await self._nc.subscribe(subjects.ALL_ALERTS, cb=self._on_message)
            log.info("stream bridge subscribed", extra={"url": self._url})

    async def _encode(self, subject: str, data: bytes) -> str | None:
        """Translate a spine message into a validated, serialised frame.

        Frames are built through the Pydantic models rather than assembled as
        dicts, so the JSON Schema clients generate from is a description of what
        is actually sent. A hand-assembled dict would drift from the schema the
        first time a field was added.

        ``None`` for a payload that does not satisfy the contract: that is a
        producer bug, and forwarding it would push the failure into every
        client at once.
        """
        received_at = datetime.now(UTC)
        try:
            if subject.startswith(f"{subjects.ROOT}.state."):
                state = ActuatorState.model_validate_json(data)
                frame: StateFrame | SensorFrame | AlertFrame = StateFrame(
                    received_at=received_at,
                    subject=subject,
                    payload=state,
                    override=await self._override_for(state.actuator_id),
                )
            elif subject.startswith(f"{subjects.ROOT}.alert."):
                frame = AlertFrame(
                    received_at=received_at,
                    subject=subject,
                    payload=SensorAlert.model_validate_json(data),
                )
            else:
                frame = SensorFrame(
                    received_at=received_at,
                    subject=subject,
                    payload=SensorReading.model_validate_json(data),
                )
        except ValidationError:
            log.warning(
                "spine payload failed contract validation; frame dropped",
                extra={"subject": subject},
                exc_info=True,
            )
            return None
        return frame.model_dump_json()

    async def _on_message(self, msg: Msg) -> None:
        encoded = await self._encode(msg.subject, msg.data)
        if encoded is None:
            return
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(encoded)
            except asyncio.QueueFull:
                # A client too slow to keep up is dropped rather than allowed to
                # back-pressure the bridge onto every other client.
                log.warning("dropping a frame for a slow subscriber")

    async def _override_for(self, actuator_id: str | None) -> OverrideContext | None:
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
        return OverrideContext(
            id=active.id,
            duty=active.duty,
            expires_at=active.expires_at,
            expires_in_s=max(
                0.0, round((active.expires_at - datetime.now(UTC)).total_seconds(), 1)
            ),
            transition=active.transition,
        )

    async def _retained_state(self) -> list[str]:
        """Every actuator's last known state, from BR_STATE, as frames.

        An ephemeral pull consumer with ``last_per_subject`` yields exactly one
        message per actuator and nothing more; it is deleted before returning
        so nothing accumulates on the broker (CLAUDE.md: a consumer that is
        left behind is not litter, it is contention). Best-effort throughout:
        a replay that fails costs the client its first frames, not its socket.
        """
        if self._nc is None:
            return []
        frames: list[str] = []
        sub = None
        try:
            js = self._nc.jetstream()
            sub = await js.pull_subscribe(
                subjects.ALL_STATE,
                stream=STATE_STREAM,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.LAST_PER_SUBJECT),
            )
            while True:
                try:
                    batch = await sub.fetch(batch=64, timeout=REPLAY_FETCH_TIMEOUT_S)
                except TimeoutError:
                    break
                for msg in batch:
                    encoded = await self._encode(msg.subject, msg.data)
                    if encoded is not None:
                        frames.append(encoded)
                if len(batch) < 64:
                    break
        except Exception:
            log.warning("could not replay retained actuator state", exc_info=True)
        finally:
            if sub is not None:
                try:
                    info = await sub.consumer_info()
                    await self._nc.jetstream().delete_consumer(STATE_STREAM, info.name)
                except Exception:
                    log.warning("ephemeral replay consumer not deleted", exc_info=True)
        return frames

    async def subscribe(self) -> asyncio.Queue[str]:
        await self._ensure_connected()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        # Retained first, then live. A change that lands between the two
        # would reach the client after its own replayed predecessor; the
        # frames carry `emitted_at`, and clients keep the newer of the two.
        for encoded in await self._retained_state():
            queue.put_nowait(encoded)
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

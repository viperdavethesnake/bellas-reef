# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""``NatsAuditSink._client``: the stale-client teardown on reconnect.

Same hazard as ``stream.py``'s ``_ensure_connected`` and ``registry.py``'s
``AssignmentPublisher.publish`` (see ``test_stream_reconnect.py`` and
``test_assignment_publisher_reconnect.py``): ``self._nc`` can be non-``None``
with ``is_connected`` False during a RECONNECTING blip that nats-py may
still recover from on its own. ``_client()`` had the identical
overwrite-without-close shape — publisher-only, so the consequence is a
leaked client per blip rather than duplicate delivery, but the leak also
outlives ``NatsAuditSink.close()``, which closes only the newest client, and
this sink is the auth audit trail.

``_client()`` is invoked on every audit event, not once at startup, which is
what makes this the highest-frequency of the three sites.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, cast

import pytest
from bellasreef_api.audit import NatsAuditSink


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeJetStream:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(
        self, subject: str, data: bytes, *, headers: dict[str, str] | None = None
    ) -> None:
        self.published.append((subject, data))


class _FakeNc:
    def __init__(self) -> None:
        self.is_connected = True
        self.closed = False
        self.js = _FakeJetStream()

    def jetstream(self) -> _FakeJetStream:
        return self.js

    async def close(self) -> None:
        self.closed = True
        self.is_connected = False


def test_a_stale_client_is_closed_before_the_next_audit_event_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = [_FakeNc(), _FakeNc()]
    calls = iter(clients)

    async def fake_connect(url: str, **kwargs: Any) -> _FakeNc:
        return next(calls)

    monkeypatch.setattr("bellasreef_api.audit.nats.connect", fake_connect)

    async def scenario() -> tuple[_FakeNc, _FakeNc, NatsAuditSink]:
        sink = NatsAuditSink("nats://unused")
        await sink("token.minted", {"client_id": "x"})
        first = clients[0]
        first.is_connected = False  # the RECONNECTING blip

        await sink("token.minted", {"client_id": "y"})
        second = clients[1]
        return first, second, sink

    first, second, sink = run(scenario)

    assert first.closed is True, "the stale client must be closed, not leaked"
    assert cast(Any, sink._nc) is second, "the sink must be left holding only the new client"
    # The event that triggered the reconnect must still land, on the fresh
    # client rather than being silently dropped.
    assert len(second.js.published) == 1

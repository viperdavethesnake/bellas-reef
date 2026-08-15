# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""NatsAuditSink publishes to the subject its category names.

Every event used to land on `bellasreef.audit.auth` regardless of what it was
— `AUDIT_CATEGORY` was the only subject the sink ever used, so device
lifecycle and override events were indistinguishable from a pairing event once
they hit the trail. This is a pure unit test of the sink itself: `NatsAuditSink`
connects lazily (`_client()`), so seeding `_nc` with a fake, already-connected
JetStream client skips the network entirely rather than requiring a real
broker for what is a one-line routing decision.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from bellasreef_api.audit import NatsAuditSink


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeJetStream:
    """Records what would have gone on the wire, in order."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(
        self, subject: str, data: bytes, *, headers: dict[str, str] | None = None
    ) -> None:
        self.published.append((subject, json.loads(data)))


class _FakeClient:
    """Stands in for `nats.aio.client.Client` — connected, never touches a socket."""

    is_connected = True

    def __init__(self, js: _FakeJetStream) -> None:
        self._js = js

    def jetstream(self) -> _FakeJetStream:
        return self._js


@pytest.fixture
def fake_js_sink() -> tuple[NatsAuditSink, list[tuple[str, dict[str, Any]]]]:
    js = _FakeJetStream()
    sink = NatsAuditSink("nats://unused")
    sink._nc = _FakeClient(js)  # type: ignore[assignment]
    return sink, js.published


def test_sink_publishes_to_the_event_category(
    fake_js_sink: tuple[NatsAuditSink, list[tuple[str, dict[str, Any]]]],
) -> None:
    sink, published = fake_js_sink

    async def scenario() -> None:
        await sink("device.unbound", {"device_id": "pi-pwm-0"}, category="config")

    run(scenario)
    subject, payload = published[-1]
    assert subject == "bellasreef.audit.config"
    assert payload["event"] == "device.unbound"


def test_sink_defaults_to_auth(
    fake_js_sink: tuple[NatsAuditSink, list[tuple[str, dict[str, Any]]]],
) -> None:
    sink, published = fake_js_sink

    async def scenario() -> None:
        await sink("token.minted", {"client_id": "x"})

    run(scenario)
    subject, _ = published[-1]
    assert subject == "bellasreef.audit.auth"

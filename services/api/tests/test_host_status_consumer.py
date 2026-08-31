# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""``HostStatusConsumer._on_message``: retained HostStatus into memory.

Unit-level like ``test_chip_consumer.py`` — a fake NATS message stands in for
the wire; no store at all, because a 30 s snapshot is process state, not a
table (design: docs/superpowers/specs/2026-08-31-hub-status-design.md). The
subscribe/retry plumbing is the same verbatim copy the other consumers share.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from bellasreef_api.registry import HostStatusConsumer
from bellasreef_contracts import HostStatus
from nats.aio.msg import Msg


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeMsg:
    def __init__(self, payload: bytes, subject: str = "bellasreef.host.status") -> None:
        self.data = payload
        self.subject = subject
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


def _status(temp_c: float | None = 46.3) -> HostStatus:
    return HostStatus(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        load_1m=0.42,
        load_5m=0.38,
        load_15m=0.33,
        cpu_count=4,
        mem_total_kb=1014464,
        mem_available_kb=445792,
        temp_c=temp_c,
        uptime_s=1692.78,
    )


def test_a_valid_message_becomes_the_latest_snapshot() -> None:
    consumer = HostStatusConsumer("nats://example.invalid:4222")
    status = _status()
    msg = _FakeMsg(status.model_dump_json().encode())

    run(lambda: consumer._on_message(cast(Msg, msg)))

    assert consumer.latest == status
    assert msg.acked


def test_the_newest_message_wins() -> None:
    consumer = HostStatusConsumer("nats://example.invalid:4222")
    first, second = _status(temp_c=40.0), _status(temp_c=50.0)

    async def scenario() -> None:
        await consumer._on_message(cast(Msg, _FakeMsg(first.model_dump_json().encode())))
        await consumer._on_message(cast(Msg, _FakeMsg(second.model_dump_json().encode())))

    run(scenario)
    assert consumer.latest == second


def test_an_invalid_message_is_ignored_and_acked() -> None:
    # Ack, don't nak: redelivering a message that failed validation once
    # fails it forever — same reasoning as the chip consumer.
    consumer = HostStatusConsumer("nats://example.invalid:4222")
    msg = _FakeMsg(b'{"not": "a host status"}')

    run(lambda: consumer._on_message(cast(Msg, msg)))

    assert consumer.latest is None
    assert msg.acked

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""``AssignmentPublisher.publish``: the stale-client teardown on reconnect.

Same hazard, and the same fix, as ``stream.py``'s ``_ensure_connected`` (see
``test_stream_reconnect.py``): ``self._nc`` can be non-``None`` with
``is_connected`` False during a RECONNECTING blip. The overwrite-without-
close idiom here used to rebuild a client without closing the old one first,
even though ``AssignmentPublisher.close()`` a few lines down already gets the
teardown right — this brings ``publish`` in line with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from bellasreef_api.registry import AssignmentPublisher
from bellasreef_contracts import DeviceAssignment


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeJs:
    async def publish(self, subject: str, payload: bytes) -> None:
        return None


class _FakeNc:
    def __init__(self) -> None:
        self.is_connected = True
        self.closed = False

    def jetstream(self) -> _FakeJs:
        return _FakeJs()

    async def close(self) -> None:
        self.closed = True
        self.is_connected = False


def _assignment() -> DeviceAssignment:
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id="led-blue",
        adopted=True,
        role="light",
        driver_type="pi-pwm",
        binding={"channel": "0"},
    )


def test_publish_closes_a_stale_client_before_reconnecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = [_FakeNc(), _FakeNc()]
    calls = iter(clients)

    async def fake_connect(url: str, **kwargs: Any) -> _FakeNc:
        return next(calls)

    monkeypatch.setattr("bellasreef_api.registry.nats.connect", fake_connect)

    async def scenario() -> tuple[_FakeNc, _FakeNc, AssignmentPublisher]:
        publisher = AssignmentPublisher("nats://unused")
        await publisher.publish(_assignment())
        first = clients[0]
        first.is_connected = False  # the RECONNECTING blip

        await publisher.publish(_assignment())
        second = clients[1]
        return first, second, publisher

    first, second, publisher = run(scenario)

    assert first.closed is True, "the stale client must be closed, not leaked"
    assert cast(Any, publisher._nc) is second

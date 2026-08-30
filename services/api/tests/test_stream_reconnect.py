# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""``StreamBridge._ensure_connected``: the stale-client teardown on reconnect.

Unit-level only, the same shape as ``test_chip_consumer.py`` and
``test_spine_unit.py``'s reconnect-callback tests — ``nats.connect`` is
monkeypatched out and a small fake stands in for the client, so no NATS
broker is needed.

The bug: nats-py reports ``is_connected is False`` on the cached client
during a routine RECONNECTING blip, while ``self._nc`` is still set to that
client. ``_ensure_connected`` used to fall through that case and build a
*second* client with a second set of the three core subscriptions, without
ever closing the first. The first client then finishes its own reconnect on
its own, and both feed ``_on_message`` — every WebSocket subscriber receives
every frame twice, permanently, until the API restarts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from bellasreef_api.stream import StreamBridge
from bellasreef_contracts import SensorReading, subjects


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeMsg:
    """Duck-types nats.aio.msg.Msg's ``.data``/``.subject`` — the whole
    surface ``StreamBridge._on_message`` reads from a delivered message."""

    def __init__(self, payload: bytes, subject: str) -> None:
        self.data = payload
        self.subject = subject


class _FakeNc:
    """Stands in for one ``nats.connect()`` result.

    ``deliver`` models what a real NATS client does: a closed client does not
    call subscription callbacks any more. That is what makes the duplicate-
    delivery test meaningful — before the fix, the stale client is never
    closed, so it goes on "delivering" (i.e. its callback is still live)
    alongside the replacement.
    """

    def __init__(self) -> None:
        self.is_connected = True
        self.closed = False
        self.subscriptions: dict[str, Callable[[Any], Coroutine[Any, Any, None]]] = {}

    async def subscribe(self, subject: str, cb: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
        self.subscriptions[subject] = cb

    async def close(self) -> None:
        self.closed = True
        self.is_connected = False

    async def deliver(self, subscribed_subject: str, msg: _FakeMsg) -> None:
        """Fire the callback registered for ``subscribed_subject`` (the
        wildcard pattern passed to ``subscribe``, e.g. ``ALL_SENSORS``) with
        ``msg``, whose own ``.subject`` may be a concrete subject beneath
        that wildcard — the same split a real NATS client makes between
        subscription routing and the delivered message's subject."""
        if self.closed:
            return
        cb = self.subscriptions.get(subscribed_subject)
        if cb is not None:
            await cb(msg)


def _reading() -> SensorReading:
    return SensorReading(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        sensor_id="probe",
        sensor_type="temp",
        value=23.9,
        unit="degC",
    )


def _fake_connect_returning(
    clients: list[_FakeNc],
) -> Callable[..., Coroutine[Any, Any, _FakeNc]]:
    calls = iter(clients)

    async def fake_connect(url: str, **kwargs: Any) -> _FakeNc:
        return next(calls)

    return fake_connect


def test_ensure_connected_closes_the_stale_client_before_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RECONNECTING blip (cached client present, ``is_connected`` False)
    must tear down the old client before a new one is built — the same
    overwrite-without-close idiom that ``registry.py``'s
    ``AssignmentPublisher.close`` already gets right."""
    clients = [_FakeNc(), _FakeNc()]
    monkeypatch.setattr("bellasreef_api.stream.nats.connect", _fake_connect_returning(clients))

    async def scenario() -> tuple[_FakeNc, _FakeNc, StreamBridge]:
        bridge = StreamBridge("nats://unused")
        await bridge.subscribe()
        first = clients[0]
        first.is_connected = False  # the RECONNECTING blip nats-py reports

        await bridge._ensure_connected()
        second = clients[1]
        return first, second, bridge

    first, second, bridge = run(scenario)

    assert first.closed is True, "the stale client must be closed, not leaked"
    assert cast(Any, bridge._nc) is second, "the bridge must be left holding only the new client"
    assert set(second.subscriptions) == {
        subjects.ALL_STATE,
        subjects.ALL_SENSORS,
        subjects.ALL_ALERTS,
    }, "the rebuilt client must re-register all three core subscriptions"


def test_a_reconnect_blip_must_not_deliver_every_frame_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-visible symptom: with the stale client left open, one message
    arriving on the wire reaches both clients' subscriptions and is forwarded
    to every WebSocket subscriber twice. Fixed, only the live client can
    still deliver, so the subscriber sees the frame once."""
    clients = [_FakeNc(), _FakeNc()]
    monkeypatch.setattr("bellasreef_api.stream.nats.connect", _fake_connect_returning(clients))

    async def scenario() -> list[str]:
        bridge = StreamBridge("nats://unused")
        queue = await bridge.subscribe()
        first = clients[0]
        first.is_connected = False

        await bridge._ensure_connected()
        second = clients[1]

        payload = _reading().model_dump_json().encode()
        msg = _FakeMsg(payload, "bellasreef.sensor.temp.probe")

        # One conceptual message on the wire. Both clients are (still)
        # subscribed to ALL_SENSORS; only a closed client should decline to
        # deliver.
        await first.deliver(subjects.ALL_SENSORS, msg)
        await second.deliver(subjects.ALL_SENSORS, msg)

        delivered: list[str] = []
        while not queue.empty():
            delivered.append(queue.get_nowait())
        return delivered

    delivered = run(scenario)

    assert len(delivered) == 1, (
        f"a reconnect blip must not duplicate delivery; got {len(delivered)} copies of one message"
    )

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""CommandPublisher wiring that does not need a real broker.

Full round trips against JetStream live in test_integration.py, gated on
BELLASREEF_TEST_NATS_URL. What is covered here is narrower and needs no
broker at all: does connect() hand nats.py a reconnected_cb that calls back
into on_reconnected. nats.connect() itself is monkeypatched out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from bellasreef_control_engine.publisher import CommandPublisher


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeNatsClient:
    """Just enough surface for connect() to accept it as the return of
    nats.connect(): a jetstream() context and an async close()."""

    def jetstream(self) -> object:
        return object()

    async def close(self) -> None:
        pass


class TestReconnectWiring:
    def test_on_reconnected_is_settable_before_connect(self) -> None:
        """The attribute must exist and be assignable pre-connect — connect()
        is where it gets handed to nats.py, not where it is created."""
        publisher = CommandPublisher("nats://unused:4222")
        assert publisher.on_reconnected is None
        publisher.on_reconnected = lambda: None  # must not raise

    def test_connect_registers_a_reconnected_cb_with_nats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_connect(url: str, **kwargs: Any) -> _FakeNatsClient:
            captured["url"] = url
            captured["reconnected_cb"] = kwargs.get("reconnected_cb")
            return _FakeNatsClient()

        monkeypatch.setattr("bellasreef_control_engine.publisher.nats.connect", fake_connect)

        publisher = CommandPublisher("nats://bench:4222")
        run(publisher.connect)

        assert captured["url"] == "nats://bench:4222"
        assert captured["reconnected_cb"] is not None

    def test_invoking_the_reconnected_cb_calls_on_reconnected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_connect(url: str, **kwargs: Any) -> _FakeNatsClient:
            captured["reconnected_cb"] = kwargs.get("reconnected_cb")
            return _FakeNatsClient()

        monkeypatch.setattr("bellasreef_control_engine.publisher.nats.connect", fake_connect)

        publisher = CommandPublisher("nats://unused:4222")
        fired = False

        def on_reconnected() -> None:
            nonlocal fired
            fired = True

        publisher.on_reconnected = on_reconnected

        async def scenario() -> None:
            await publisher.connect()
            reconnected_cb = captured["reconnected_cb"]
            assert reconnected_cb is not None
            await reconnected_cb()

        asyncio.run(scenario())
        assert fired is True

    def test_no_on_reconnected_set_is_a_quiet_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A publisher nobody wired a handler onto (e.g. the engine ran with
        no spine at all) must not blow up when nats.py calls the cb back."""
        captured: dict[str, Any] = {}

        async def fake_connect(url: str, **kwargs: Any) -> _FakeNatsClient:
            captured["reconnected_cb"] = kwargs.get("reconnected_cb")
            return _FakeNatsClient()

        monkeypatch.setattr("bellasreef_control_engine.publisher.nats.connect", fake_connect)

        publisher = CommandPublisher("nats://unused:4222")

        async def scenario() -> None:
            await publisher.connect()
            reconnected_cb = captured["reconnected_cb"]
            assert reconnected_cb is not None
            await reconnected_cb()  # must not raise

        asyncio.run(scenario())

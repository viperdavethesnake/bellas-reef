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
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ActuatorState, PwmLevel, StateReason
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


class _FakeConnectedNc:
    """Minimal nats.py client stand-in for the ``connected`` property tests
    below: only needs an ``is_connected`` attribute, toggled to simulate the
    client's own reconnect state machine. nats-py maintains
    ``Client.is_connected`` across a RECONNECTING window on its own — the
    real bug (2026-08-23 finding 8) was that ``CommandPublisher.connected``
    used to ask ``_js is not None`` instead, which stays True through that
    same window."""

    def __init__(self) -> None:
        self.is_connected = True


@pytest.fixture
def publisher_with_fake_nc() -> tuple[CommandPublisher, _FakeConnectedNc]:
    """A CommandPublisher wired directly to a fake client, bypassing
    connect() — same pattern as TestSubscribeStates below (``publisher._nc =
    fake_nc  # type: ignore[assignment]``). ``_js`` is set to a non-None
    sentinel too, so a test that fails to update ``connected`` back to
    ``self._js is not None`` still passes were it not for the client's own
    ``is_connected`` — proving the fix asks the right object."""
    publisher = CommandPublisher("nats://unused:4222")
    nc = _FakeConnectedNc()
    publisher._nc = nc  # type: ignore[assignment]
    publisher._js = object()  # type: ignore[assignment]
    return publisher, nc


class TestConnectedProperty:
    """``connected`` (Task 3, 2026-08-23 finding 8): must reflect the
    client's own ``is_connected``, not merely whether ``_js`` was ever set.
    ``_js`` stays non-None through a RECONNECTING window, which is exactly
    the state that let a PubAck timeout crash-loop the engine while
    ``health()`` kept reporting "spine ok" for the whole outage."""

    def test_connected_reflects_the_client_not_the_handle(
        self, publisher_with_fake_nc: tuple[CommandPublisher, _FakeConnectedNc]
    ) -> None:
        publisher, nc = publisher_with_fake_nc
        nc.is_connected = False  # RECONNECTING: _js is still non-None
        assert publisher.connected is False

    def test_connected_true_when_the_client_says_so(
        self, publisher_with_fake_nc: tuple[CommandPublisher, _FakeConnectedNc]
    ) -> None:
        publisher, _nc = publisher_with_fake_nc
        assert publisher.connected is True

    def test_connected_false_before_connect(self) -> None:
        publisher = CommandPublisher("nats://unused:4222")
        assert publisher.connected is False


class _FakeMsg:
    def __init__(self, payload: bytes, subject: str) -> None:
        self.data = payload
        self.subject = subject


class _FakeNc:
    """Just enough of nats.py's client for subscribe() to capture its
    callback — same shape as test_assignments.py's fake, duplicated rather
    than imported since that module's fakes are test-local, not exported."""

    def __init__(self) -> None:
        self.cb: Callable[[_FakeMsg], Coroutine[Any, Any, None]] | None = None

    async def subscribe(
        self, subject: str, cb: Callable[[_FakeMsg], Coroutine[Any, Any, None]]
    ) -> None:
        self.cb = cb


def _state(actuator_id: str, *, reason: StateReason) -> ActuatorState:
    now = datetime.now(UTC)
    return ActuatorState(
        message_id=uuid4(),
        emitted_at=now,
        source="hardware-io",
        actuator_id=actuator_id,
        level=PwmLevel(duty=0.0),
        reason=reason,
        since=now,
    )


class TestSubscribeStates:
    """subscribe_states copies subscribe_assignments's contract exactly: core
    pub/sub (no durable to leak — a JetStream publish traverses core subjects
    too), and parsing/handling guarded separately so a malformed payload or a
    raising handler cannot kill the subscription."""

    def test_subscribe_states_survives_a_raising_handler(self) -> None:
        good = _state("pca9685-0", reason="safe_state")
        msg = _FakeMsg(good.model_dump_json().encode(), "bellasreef.state.pca9685-0")

        def handler(state: ActuatorState) -> None:
            raise RuntimeError("boom")

        publisher = CommandPublisher("nats://unused:4222")
        fake_nc = _FakeNc()
        publisher._nc = fake_nc  # type: ignore[assignment]

        asyncio.run(publisher.subscribe_states(handler))

        assert fake_nc.cb is not None
        asyncio.run(fake_nc.cb(msg))  # must not raise out of the callback

    def test_subscribe_states_drops_a_malformed_payload_without_calling_the_handler(
        self,
    ) -> None:
        msg = _FakeMsg(b"not json", "bellasreef.state.junk")
        calls: list[ActuatorState] = []

        publisher = CommandPublisher("nats://unused:4222")
        fake_nc = _FakeNc()
        publisher._nc = fake_nc  # type: ignore[assignment]

        asyncio.run(publisher.subscribe_states(calls.append))

        assert fake_nc.cb is not None
        asyncio.run(fake_nc.cb(msg))  # must not raise; must not reach the handler

        assert calls == []

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The ledger is pure state: assignments in, adopted-set out."""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import DeviceAssignment
from bellasreef_control_engine.assignments import AssignmentLedger
from bellasreef_control_engine.publisher import CommandPublisher
from nats.js.errors import NotFoundError


def _assignment(device_id: str, *, adopted: bool) -> DeviceAssignment:
    kwargs: dict[str, Any] = {}
    if adopted:
        kwargs = {"driver_type": "pi-pwm", "binding": {"channel": "0"}, "role": "light"}
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=adopted,
        **kwargs,
    )


class TestAssignmentLedger:
    def test_starts_empty(self) -> None:
        assert AssignmentLedger().adopted == frozenset()

    def test_adoption_adds(self) -> None:
        ledger = AssignmentLedger()
        ledger.apply(_assignment("led-blue", adopted=True))
        assert ledger.is_adopted("led-blue")
        assert ledger.adopted == frozenset({"led-blue"})

    def test_tombstone_removes(self) -> None:
        ledger = AssignmentLedger()
        ledger.apply(_assignment("led-blue", adopted=True))
        ledger.apply(_assignment("led-blue", adopted=False))
        assert not ledger.is_adopted("led-blue")

    def test_tombstone_for_unknown_device_is_a_no_op(self) -> None:
        ledger = AssignmentLedger()
        ledger.apply(_assignment("led-blue", adopted=False))
        assert ledger.adopted == frozenset()


class _FakeMsg:
    def __init__(self, payload: bytes, subject: str) -> None:
        self.data = payload
        self.subject = subject
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _FakeSub:
    def __init__(self, batches: list[list[_FakeMsg]]) -> None:
        self._batches = batches

    async def fetch(self, n: int, timeout: float) -> list[_FakeMsg]:
        if not self._batches:
            raise TimeoutError
        return self._batches.pop(0)

    async def unsubscribe(self) -> None:
        pass


class _FakeJs:
    def __init__(self, batches: list[list[_FakeMsg]]) -> None:
        self._batches = batches

    async def pull_subscribe(self, subject: str, durable: object, config: object) -> _FakeSub:
        return _FakeSub(self._batches)


class _FakeNc:
    def __init__(self) -> None:
        self.cb: Callable[[_FakeMsg], Coroutine[Any, Any, None]] | None = None

    async def subscribe(
        self, subject: str, cb: Callable[[_FakeMsg], Coroutine[Any, Any, None]]
    ) -> None:
        self.cb = cb


def test_drain_feeds_ledger_and_skips_garbage() -> None:
    good = _assignment("led-blue", adopted=True)
    msgs = [
        _FakeMsg(good.model_dump_json().encode(), "bellasreef.assignment.led-blue"),
        _FakeMsg(b"not json", "bellasreef.assignment.junk"),
    ]
    publisher = CommandPublisher("nats://unused:4222")
    publisher._js = _FakeJs([msgs])  # type: ignore[assignment]
    ledger = AssignmentLedger()

    loaded = asyncio.run(publisher.load_assignments(ledger))

    assert loaded is True
    assert ledger.adopted == frozenset({"led-blue"})
    assert all(m.acked for m in msgs)


def test_subscribe_assignments_survives_a_raising_handler() -> None:
    good = _assignment("led-blue", adopted=True)
    msg = _FakeMsg(good.model_dump_json().encode(), "bellasreef.assignment.led-blue")

    def handler(assignment: DeviceAssignment) -> None:
        raise RuntimeError("boom")

    publisher = CommandPublisher("nats://unused:4222")
    fake_nc = _FakeNc()
    publisher._nc = fake_nc  # type: ignore[assignment]

    asyncio.run(publisher.subscribe_assignments(handler))

    assert fake_nc.cb is not None
    asyncio.run(fake_nc.cb(msg))  # must not raise out of the callback


class _FakeJsNotFound:
    """A stream that has not been provisioned yet — pull_subscribe refuses."""

    async def pull_subscribe(self, subject: str, durable: object, config: object) -> _FakeSub:
        raise NotFoundError


def test_unprovisioned_stream_warns_once_then_throttles(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_loop retries load_assignments every loop_interval_s (1s by default)
    until the stream provisions. Without throttling that is a warning a
    second; this asserts the first sighting logs and the throttle window
    is respected, while every call still returns False."""
    fake_now = [0.0]
    # Patches the shared stdlib `time` module object, not a reference held by
    # bellasreef_control_engine.publisher — mypy --strict forbids reaching
    # into another module's attributes that aren't in its __all__, and this
    # works regardless because `import time` there resolves to this same
    # module instance.
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    publisher = CommandPublisher("nats://unused:4222")
    publisher._js = _FakeJsNotFound()  # type: ignore[assignment]
    ledger = AssignmentLedger()

    with caplog.at_level(logging.WARNING, logger="bellasreef_control_engine.publisher"):
        assert asyncio.run(publisher.load_assignments(ledger)) is False  # t=0: warns
        assert asyncio.run(publisher.load_assignments(ledger)) is False  # t=0: throttled

        fake_now[0] = 30.0
        assert asyncio.run(publisher.load_assignments(ledger)) is False  # t=30: throttled

        fake_now[0] = 61.0
        assert asyncio.run(publisher.load_assignments(ledger)) is False  # t=61: warns again

    warnings = [r for r in caplog.records if "not provisioned" in r.message]
    assert len(warnings) == 2

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The ledger is pure state: assignments in, adopted-set out."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from bellasreef_contracts import DeviceAssignment
from bellasreef_control_engine.assignments import AssignmentLedger
from bellasreef_control_engine.publisher import CommandPublisher


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

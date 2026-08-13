# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The ledger is pure state: assignments in, adopted-set out."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from bellasreef_contracts import DeviceAssignment
from bellasreef_control_engine.assignments import AssignmentLedger


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

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""build_from_assignments against a drained assignment list that can repeat.

Spine.read_assignments() drains a pull-subscribe with
DeliverPolicy.LAST_PER_SUBJECT, unsubscribed once the drain is done. A deploy
recreates api and hardware-io together, and the api's startup lifespan
republishes every adopted assignment on the same subject a hardware-io boot is
mid-drain of — so the drain can observe one device_id twice: the retained
message plus its own fresher echo on the same subject.

Nothing about ordering makes the earlier copy wrong to have seen — it is
simply stale the instant the newer one lands. So the fix here is not "the
drain must never duplicate" (spine.py's job, untouched by this file) but
"the factory must never act on a device_id twice." Last occurrence wins,
which is safe precisely because a later message on the same subject is never
older than an earlier one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bellasreef_contracts import DeviceAssignment
from bellasreef_hardware_io.factory import build_from_assignments


def _pipwm(device_id: str, *, adopted: bool, channel: str = "0") -> DeviceAssignment:
    if adopted:
        return DeviceAssignment(
            message_id=uuid4(),
            emitted_at=datetime.now(UTC),
            source="api",
            device_id=device_id,
            adopted=True,
            role="light",
            driver_type="pi-pwm",
            binding={"channel": channel},
        )
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=False,
    )


def test_the_same_adopted_assignment_twice_builds_exactly_one_device() -> None:
    """The crash scenario: a retained message plus its own republished echo."""
    assignments = [_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-1", adopted=True)]

    actuators, sensors = build_from_assignments(assignments)

    assert [a.registration.actuator_id for a in actuators] == ["pi-pwm-1"]
    assert sensors == []


def test_an_adopted_assignment_followed_by_its_tombstone_builds_nothing() -> None:
    """Last wins: the unadopted echo landed after the adopted one, so the
    device is free again by the time the factory acts on it."""
    assignments = [_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-1", adopted=False)]

    actuators, sensors = build_from_assignments(assignments)

    assert actuators == []
    assert sensors == []


def test_an_unadopted_assignment_followed_by_a_re_adopt_builds_one() -> None:
    """Re-adopt echo order: the later adopted copy is the one that counts."""
    assignments = [_pipwm("pi-pwm-1", adopted=False), _pipwm("pi-pwm-1", adopted=True)]

    actuators, sensors = build_from_assignments(assignments)

    assert [a.registration.actuator_id for a in actuators] == ["pi-pwm-1"]
    assert sensors == []


def test_dedup_preserves_order_of_last_occurrence_across_distinct_devices() -> None:
    """A duplicate of the first device must not reorder devices seen once."""
    assignments = [
        _pipwm("pi-pwm-1", adopted=True),
        _pipwm("pi-pwm-2", adopted=True),
        _pipwm("pi-pwm-1", adopted=True),
    ]

    actuators, _ = build_from_assignments(assignments)

    assert [a.registration.actuator_id for a in actuators] == ["pi-pwm-2", "pi-pwm-1"]


def test_a_collapsed_duplicate_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="bellasreef_hardware_io.factory"):
        build_from_assignments([_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-1", adopted=True)])

    assert any("dedup" in r.getMessage() or "duplicate" in r.getMessage() for r in caplog.records)

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""``_build_from_registry``: every actuator is opened before it is registered.

Stage 2 on 2026-08-17 found a driver whose chip setup had never run in
production because the app duck-typed ``driver.open()`` and that driver had
none. ``open()`` is a required Protocol member now; these tests pin the
app-side half — that the hook is called, once, on every actuator built, and
that one actuator failing to open costs that actuator and nothing else.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bellasreef_contracts import ActuatorRegistration, BinaryLevel
from bellasreef_hardware_io import FakeActuator
from bellasreef_hardware_io import app as app_module
from bellasreef_hardware_io.app import HardwareIO
from bellasreef_hardware_io.factory import BuiltActuator

OFF = BinaryLevel(on=False)


def _registration(actuator_id: str) -> ActuatorRegistration:
    return ActuatorRegistration(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=actuator_id,
        actuator_class="binary",
        role="outlet",
        driver_id="fake-actuator",
        control_authority="authoritative",
        failsafe_capable=True,
        transport="local",
        safe_state=OFF,
        max_runtime_s=3600.0,
        heartbeat_timeout_s=30.0,
    )


class _Spine:
    """Just enough spine for ``_build_from_registry`` to run: nothing assigned."""

    async def read_assignments(self) -> list[object]:
        return []


def _service_building(monkeypatch: pytest.MonkeyPatch, *actuators: FakeActuator) -> HardwareIO:
    """A service whose factory returns exactly ``actuators``, no sensors."""
    built = [BuiltActuator(a, _registration(a.actuator_id)) for a in actuators]
    monkeypatch.setattr(
        app_module, "build_from_assignments", lambda assignments, *, open_i2c: (built, [])
    )
    service = HardwareIO(metrics_port=0)
    service.spine = _Spine()  # type: ignore[assignment]
    return service


def test_every_built_actuator_is_opened_once(monkeypatch: pytest.MonkeyPatch) -> None:
    a = FakeActuator("light-0", OFF)
    b = FakeActuator("light-1", OFF)
    service = _service_building(monkeypatch, a, b)

    asyncio.run(service._build_from_registry())

    assert (a.opened, b.opened) == (1, 1)
    assert {r.actuator_id for r in service._registrations} == {"light-0", "light-1"}


def test_an_actuator_that_fails_to_open_is_skipped_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two lights that could not open once took the temperature probe down
    with them. A light that cannot open is dark either way; the rest of the
    hub must still come up."""
    broken = FakeActuator("light-0", OFF)
    broken.open_raises = True
    fine = FakeActuator("light-1", OFF)
    service = _service_building(monkeypatch, broken, fine)

    asyncio.run(service._build_from_registry())

    assert [r.actuator_id for r in service._registrations] == ["light-1"]
    assert fine.opened == 1

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""hardware-io — sole owner of the Pi's hardware.

Session 2 scope: the safety framework and fake drivers. Real drivers (PCA9685,
DS18B20) and the NATS wiring land next, against the interlocks proven here.
"""

from bellasreef_hardware_io.fakes import FakeActuator, FakeSensor
from bellasreef_hardware_io.safety import (
    CommandOutcome,
    InterlockSupervisor,
    SafetyEvent,
    TripReason,
)

__all__ = [
    "CommandOutcome",
    "FakeActuator",
    "FakeSensor",
    "InterlockSupervisor",
    "SafetyEvent",
    "TripReason",
]

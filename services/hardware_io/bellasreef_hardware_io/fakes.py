# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Fake drivers.

Shipped as library code rather than test fixtures because the control engine
will need them too, and because a fake that only exists in a test file drifts
away from the Protocol it is supposed to satisfy.

Every fake can misbehave on demand. The failure paths are the ones worth
testing, and a real tank will exercise them for you unprompted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from uuid import UUID, uuid4

from bellasreef_contracts import ActuatorLevel, BinaryLevel, PwmLevel
from bellasreef_contracts.driver import (
    CalibrationPoint,
    CalibrationResult,
    SensorSample,
)

from bellasreef_hardware_io.drivers.dimming import snap_duty

__all__ = ["FakeActuator", "FakeSensor", "SnappingFakeActuator"]


class FakeActuator:
    """An in-memory actuator satisfying :class:`ActuatorDriver`.

    Misbehaviour switches:

    ``stuck``
        ``apply`` and ``drive_safe`` accept the call and report success, but the
        output never moves. Models a welded relay — the case where "commanded"
        and "actual" diverge and only ``read_back`` can tell.
    ``fail_safe_raises``
        ``drive_safe`` raises. The supervisor must not be left in a state where
        one broken driver stops the others going safe.
    ``apply_delay_s``
        ``apply`` takes this long, for testing timeouts.
    ``open_raises``
        ``open`` raises. Models a channel that cannot be brought up — an
        unexported RP1 channel whose udev rule never ran, a PCA9685 that
        answers no I²C — which the app must skip without taking the rest of
        the hub down with it.
    ``apply_raises``
        ``apply`` raises this exception instead of applying. Models a chip
        that physically drops off the bus mid-command (the PCA9685 on
        2026-08-15) — the command consumer must nak, not crash.
    """

    def __init__(
        self,
        actuator_id: str,
        safe_state: ActuatorLevel,
        *,
        driver_id: str = "fake-actuator",
    ) -> None:
        self._actuator_id = actuator_id
        self._safe_state = safe_state
        self._driver_id = driver_id

        self.level: ActuatorLevel = safe_state
        self.applied: list[ActuatorLevel] = []
        self.safe_calls: int = 0
        self.opened: int = 0

        self.stuck: bool = False
        self.fail_safe_raises: bool = False
        self.apply_delay_s: float = 0.0
        self.open_raises: bool = False
        self.apply_raises: BaseException | None = None

    @property
    def driver_id(self) -> str:
        return self._driver_id

    @property
    def actuator_id(self) -> str:
        return self._actuator_id

    @property
    def safe_state(self) -> ActuatorLevel:
        return self._safe_state

    async def open(self) -> None:
        if self.open_raises:
            raise OSError(f"{self._actuator_id} cannot be brought up")
        self.opened += 1

    async def apply(self, level: ActuatorLevel) -> None:
        if self.apply_delay_s:
            await asyncio.sleep(self.apply_delay_s)
        if self.apply_raises is not None:
            raise self.apply_raises
        self.applied.append(level)
        if not self.stuck:
            self.level = level

    async def drive_safe(self) -> None:
        self.safe_calls += 1
        if self.fail_safe_raises:
            raise RuntimeError(f"{self._actuator_id}: drive_safe failed")
        if not self.stuck:
            self.level = self._safe_state

    async def read_back(self) -> ActuatorLevel | None:
        return self.level

    def effective_level(self, level: ActuatorLevel) -> ActuatorLevel:
        """Identity: this fake models no hardware-specific narrowing.

        Real PWM drivers narrow a commanded level to what actually reaches
        the pin (dimming.py's ``snap_duty``, since safety.py's 2026-08-29
        max-runtime-keys-on-effective-level fix); this generic fake is also
        used for plain binary actuators that have no such narrowing, so the
        honest generic answer is "whatever was asked for". A test exercising
        the snap-band behaviour wants :class:`SnappingFakeActuator` instead.
        """
        return level

    # ---- test helpers -----------------------------------------------------

    def is_safe(self) -> bool:
        """Live query, deliberately a method rather than a property.

        The value changes under the caller as the supervisor drives the output.
        A property reads as a stable attribute and type checkers narrow it as
        one, so `assert not x.is_safe` earlier in a test would silently poison
        every later `assert x.is_safe`.
        """
        return self.level == self._safe_state


class SnappingFakeActuator:
    """A minimal PWM-class driver satisfying ``ActuatorDriver`` by hand.

    Mirrors the real PCA9685/RP1 drivers' round-trip: ``apply()`` snaps duty
    the same way ``duty_to_counts``/``duty_to_ns`` do (dimming.py's
    ``snap_duty`` — under 8% snaps to 0), ``read_back()`` reports what was
    actually written rather than what was asked for, and ``effective_level()``
    is the same snap computed *without* touching state — the pure prediction
    safety.py's runtime-cap clock keys off. No real hardware involved; this is
    the shape of the snap-band bug, not the silicon.
    """

    def __init__(self, actuator_id: str, safe_state: PwmLevel) -> None:
        self._actuator_id = actuator_id
        self._safe_state = safe_state
        self._level = safe_state

    @property
    def driver_id(self) -> str:
        return "fake-pwm"

    @property
    def actuator_id(self) -> str:
        return self._actuator_id

    @property
    def safe_state(self) -> ActuatorLevel:
        return self._safe_state

    async def open(self) -> None:
        pass

    async def apply(self, level: ActuatorLevel) -> None:
        if not isinstance(level, PwmLevel):
            raise TypeError(f"{self._actuator_id} is a PWM channel; got {type(level).__name__}")
        self._level = PwmLevel(duty=snap_duty(level.duty))

    async def drive_safe(self) -> None:
        self._level = self._safe_state

    async def read_back(self) -> ActuatorLevel | None:
        return self._level

    def effective_level(self, level: ActuatorLevel) -> ActuatorLevel:
        if not isinstance(level, PwmLevel):
            raise TypeError(f"{self._actuator_id} is a PWM channel; got {type(level).__name__}")
        return PwmLevel(duty=snap_duty(level.duty))


class FakeSensor:
    """An in-memory sensor satisfying :class:`SensorDriver`.

    ``stall_s`` exists to exercise the rule that a slow bus must not dictate
    control-loop timing: set it above ``read_timeout_s`` and the caller should
    record a fault rather than wait.
    """

    def __init__(
        self,
        driver_id: str,
        sensor_type: str,
        *,
        value: float = 25.0,
        unit: str = "degC",
        poll_interval_s: float = 1.0,
        read_timeout_s: float = 2.0,
    ) -> None:
        self._driver_id = driver_id
        self._sensor_type = sensor_type
        self._unit = unit
        self._poll_interval_s = poll_interval_s
        self._read_timeout_s = read_timeout_s

        self.value = value
        self.faulty: bool = False
        self.stall_s: float = 0.0
        self.reads: int = 0
        self.calibration_id: UUID | None = None

    @property
    def driver_id(self) -> str:
        return self._driver_id

    @property
    def sensor_type(self) -> str:
        return self._sensor_type

    @property
    def unit(self) -> str:
        return self._unit

    @property
    def poll_interval_s(self) -> float:
        return self._poll_interval_s

    @property
    def read_timeout_s(self) -> float:
        return self._read_timeout_s

    async def read(self) -> SensorSample:
        self.reads += 1
        if self.stall_s:
            await asyncio.sleep(self.stall_s)
        if self.faulty:
            # Contract: never raise on hardware failure. Losing a probe is an
            # operating condition, not an exception.
            return SensorSample(
                value=None,
                raw=None,
                unit=self._unit,
                quality="fault",
                calibration_id=self.calibration_id,
            )
        return SensorSample(
            value=self.value,
            raw=self.value,
            unit=self._unit,
            quality="ok",
            calibration_id=self.calibration_id,
        )

    async def calibrate(self, points: Sequence[CalibrationPoint]) -> CalibrationResult:
        if not points:
            raise ValueError("calibration needs at least one reference point")
        cal_id = uuid4()
        self.calibration_id = cal_id
        offset = sum(p.reference - p.raw for p in points) / len(points)
        return CalibrationResult(
            calibration_id=cal_id,
            coefficients=(offset, 1.0),
            residual=0.0,
            points=tuple(points),
        )


def binary_safe_off() -> BinaryLevel:
    """The usual safe state: de-energised."""
    return BinaryLevel(on=False)


def pwm_dark() -> PwmLevel:
    """The usual safe state for a light channel."""
    return PwmLevel(duty=0.0)

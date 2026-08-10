# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Hardware driver interface.

This is the contract that cannot be retrofitted. Once drivers accumulate against
a loose interface, tightening it means rewriting all of them — so it is defined
before the first driver exists, and stays stable even with one implementation.

Two rules below are encoded in the types rather than left to documentation,
because both are drawn from measured facts about the target board and both are
easy to violate by accident. See "Verified host facts" in CLAUDE.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bellasreef_contracts.messages import ActuatorLevel, DeviceId

__all__ = [
    "ActuatorDriver",
    "CalibrationPoint",
    "CalibrationResult",
    "GpioLine",
    "I2CAddress",
    "OneWireDevice",
    "SensorDriver",
    "SensorSample",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------ addressing


class GpioLine(_Frozen):
    """A GPIO line addressed by chip **label**, never by chip index.

    ``/dev/gpiochipN`` numbering has moved between kernel releases on this
    board. On the target, the 40-pin header is ``pinctrl-rp1`` (54 lines); the
    ``gpio-brcmstb`` chips are internal SoC lines and are *not* header pins.
    Resolving by label is therefore the only stable addressing, and this model
    exists so that an integer index cannot be passed at all.
    """

    chip_label: str = Field(min_length=1, examples=["pinctrl-rp1"])
    offset: int = Field(ge=0, description="Line offset within the labelled chip")


class I2CAddress(_Frozen):
    """A 7-bit I²C address on a numbered bus."""

    bus: int = Field(ge=0, examples=[1])
    address: int = Field(ge=0x03, le=0x77)


class OneWireDevice(_Frozen):
    """A 1-Wire device by its bus id, e.g. ``28-0000075d1b2c``.

    The read path is sysfs (``/sys/bus/w1/devices/<id>/w1_slave``) because that
    is the only interface the ``w1-therm`` kernel driver exposes — there is no
    character-device equivalent. This is distinct from the forbidden *sysfs
    GPIO* (``/sys/class/gpio``), which is deprecated and absent on this board.
    """

    device_id: str = Field(pattern=r"^[0-9a-f]{2}-[0-9a-f]{12}$")


# ------------------------------------------------------------ calibration


class CalibrationPoint(_Frozen):
    """One reference observation used to fit a calibration."""

    raw: float
    reference: float
    unit: str = Field(min_length=1, max_length=16)


class CalibrationResult(_Frozen):
    """Outcome of a calibration, persisted to ``calibration_records``."""

    calibration_id: UUID
    coefficients: tuple[float, ...]
    residual: float = Field(ge=0.0)
    points: tuple[CalibrationPoint, ...]


# ------------------------------------------------------------ samples


class SensorSample(_Frozen):
    """A driver-level reading, before it becomes a wire ``SensorReading``.

    Drivers report ``raw`` alongside the calibrated ``value`` so a bad
    calibration can be diagnosed after the fact rather than silently baked in.
    """

    value: float | None
    raw: float | None
    unit: str = Field(min_length=1, max_length=16)
    quality: Literal["ok", "stale", "fault"]
    calibration_id: UUID | None = None


# ------------------------------------------------------------ interfaces


@runtime_checkable
class SensorDriver(Protocol):
    """A pollable sensor.

    **Timing rule.** The driver declares its own cadence; the caller obeys it
    and polls every driver as an independent task. A driver must never make the
    control loop wait on its bus.

    This matters concretely: a DS18B20 blocks roughly 750 ms per conversion at
    12-bit resolution, and the 1-Wire bus is serialized — N probes cost N×750 ms
    if read naively. That cost belongs inside the driver (offload blocking reads
    with ``asyncio.to_thread``, arbitrate the shared bus internally, cache the
    last good sample), never in the scheduler that decides whether a heater runs.

    An implementation whose ``read`` can block the event loop is a contract
    violation regardless of whether it returns correct values.
    """

    @property
    def driver_id(self) -> DeviceId:
        """Stable identifier, unique within a hardware-io instance."""
        ...

    @property
    def sensor_type(self) -> DeviceId:
        """Subject token for telemetry, e.g. ``temp``, ``ph``."""
        ...

    @property
    def poll_interval_s(self) -> float:
        """Cadence this driver wants to be polled at. Must be > 0.

        Set it from the physics, not from what the control loop would like: a
        DS18B20 cannot honestly produce fresh values faster than its conversion
        time.
        """
        ...

    @property
    def read_timeout_s(self) -> float:
        """Deadline for a single :meth:`read`. Must exceed the worst-case
        conversion time, and exceeding it yields ``quality='fault'``, never a
        stale value presented as fresh."""
        ...

    async def read(self) -> SensorSample:
        """Take one sample.

        Must not raise on hardware failure — return ``quality='fault'`` instead.
        Losing a probe is an expected operating condition, not an exception.
        """
        ...

    async def calibrate(self, points: Sequence[CalibrationPoint]) -> CalibrationResult:
        """Fit and persist a calibration from reference observations."""
        ...


@runtime_checkable
class ActuatorDriver(Protocol):
    """A commandable output.

    The driver owns the last line of defence. ``drive_safe`` must work when the
    spine is down, the control engine is gone, and no command has arrived —
    which is exactly when it will be called.
    """

    @property
    def driver_id(self) -> DeviceId: ...

    @property
    def actuator_id(self) -> DeviceId: ...

    @property
    def safe_state(self) -> ActuatorLevel:
        """The level this output takes when anything goes wrong."""
        ...

    async def apply(self, level: ActuatorLevel) -> None:
        """Drive the output to ``level``.

        The caller has already checked command expiry and interlocks. The driver
        still validates that ``level.kind`` matches its class.
        """
        ...

    async def drive_safe(self) -> None:
        """Force :attr:`safe_state` immediately.

        Called on heartbeat loss, interlock trip, and shutdown. Must not depend
        on NATS, the control engine, or the database.
        """
        ...

    async def read_back(self) -> ActuatorLevel | None:
        """Actual output level if the hardware can report it, else ``None``.

        Distinguishing "commanded" from "confirmed" is what makes a stuck relay
        detectable.
        """
        ...

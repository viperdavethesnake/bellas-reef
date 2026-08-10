# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""DS18B20 temperature driver.

The first real driver, and the one the driver-interface timing rule was written
for. Measured on the target hardware, a single read costs **831 ms** — above the
~750 ms datasheet conversion time at 12-bit resolution — and the 1-Wire bus is
serialized, so N probes read naively cost N × 831 ms.

That cost is absorbed here and never exposed to the caller:

* the blocking sysfs read runs in a worker thread, so the event loop keeps
  running;
* probes sharing a bus master serialize against *each other* through a lock
  owned by this module, not against anything else in the process;
* a read that overruns ``read_timeout_s`` yields ``quality="fault"`` rather than
  a stale value wearing a fresh timestamp.

Read path is sysfs because that is the only interface ``w1-therm`` exposes.
This is not the forbidden sysfs GPIO.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from bellasreef_contracts.driver import (
    CalibrationPoint,
    CalibrationResult,
    OneWireDevice,
    SensorSample,
)

__all__ = ["DS18B20", "W1_ROOT", "discover_probes"]

W1_ROOT = Path("/sys/bus/w1/devices")

#: The DS18B20 powers up holding exactly 85.0 °C in its scratchpad. A read of
#: 85000 with a good CRC therefore almost always means "converted nothing yet",
#: not "the tank is at 85 °C" — which in a reef context is not a plausible
#: reading anyway. Treated as a fault rather than published as truth.
_POWER_ON_RESET_MILLIC = 85000

_CRC_RE = re.compile(r"crc=[0-9a-f]{2}\s+(YES|NO)\s*$", re.MULTILINE)
_TEMP_RE = re.compile(r"\bt=(-?\d+)\s*$", re.MULTILINE)

#: One lock per bus master. Probes on the same 1-Wire bus must not talk over
#: each other; probes on different bus masters need not wait.
_BUS_LOCKS: dict[str, asyncio.Lock] = {}


def _bus_lock(bus_master: str) -> asyncio.Lock:
    lock = _BUS_LOCKS.get(bus_master)
    if lock is None:
        lock = asyncio.Lock()
        _BUS_LOCKS[bus_master] = lock
    return lock


def discover_probes(root: Path = W1_ROOT) -> tuple[OneWireDevice, ...]:
    """Every DS18B20 currently enumerated on the bus.

    Family code ``28`` is the DS18B20. Other families on the same bus (``10``
    for DS18S20, ``22`` for DS1822) are deliberately not claimed here.
    """
    if not root.is_dir():
        return ()
    return tuple(
        OneWireDevice(device_id=p.name) for p in sorted(root.iterdir()) if p.name.startswith("28-")
    )


class DS18B20:
    """One DS18B20 probe. Satisfies :class:`SensorDriver`."""

    def __init__(
        self,
        device: OneWireDevice,
        *,
        driver_id: str | None = None,
        root: Path = W1_ROOT,
        poll_interval_s: float = 5.0,
        read_timeout_s: float = 2.0,
        bus_master: str = "w1_bus_master1",
        offset_c: float = 0.0,
        calibration_id: UUID | None = None,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
        if read_timeout_s <= 0:
            raise ValueError("read_timeout_s must be > 0")
        # A timeout under the conversion time guarantees permanent faults.
        # Measured cost on this hardware is 831 ms; anything below that is a
        # configuration error, not a tight deadline.
        if read_timeout_s < 1.0:
            raise ValueError(
                f"read_timeout_s={read_timeout_s} is below the measured 831 ms "
                "conversion cost; the probe could never succeed"
            )

        self._device = device
        self._driver_id = driver_id or f"ds18b20-{device.device_id}"
        self._path = root / device.device_id / "w1_slave"
        self._poll_interval_s = poll_interval_s
        self._read_timeout_s = read_timeout_s
        self._bus_master = bus_master

        self._offset_c = offset_c
        self._calibration_id = calibration_id

    # -------------------------------------------------------------- protocol

    @property
    def driver_id(self) -> str:
        return self._driver_id

    @property
    def sensor_type(self) -> str:
        return "temp"

    @property
    def unit(self) -> str:
        return "degC"

    @property
    def poll_interval_s(self) -> float:
        return self._poll_interval_s

    @property
    def read_timeout_s(self) -> float:
        return self._read_timeout_s

    async def read(self) -> SensorSample:
        """Take one sample. Never raises on hardware failure."""
        try:
            async with _bus_lock(self._bus_master):
                raw_text = await asyncio.wait_for(
                    asyncio.to_thread(self._blocking_read),
                    timeout=self._read_timeout_s,
                )
        except (TimeoutError, OSError):
            # Probe unplugged, bus wedged, or conversion overran. All are
            # operating conditions on a wet installation, not exceptions.
            return self._fault()

        return self._parse(raw_text)

    async def calibrate(self, points: Sequence[CalibrationPoint]) -> CalibrationResult:
        """Fit a single-point offset.

        A DS18B20 is factory-trimmed and linear across a reef's range; the
        useful correction is an offset against a reference thermometer, not a
        curve. Fitting a slope to a handful of points near 25 °C would model
        noise.
        """
        if not points:
            raise ValueError("calibration needs at least one reference point")

        offset = sum(p.reference - p.raw for p in points) / len(points)
        residual = max(abs((p.raw + offset) - p.reference) for p in points)

        self._offset_c = offset
        self._calibration_id = uuid4()
        return CalibrationResult(
            calibration_id=self._calibration_id,
            coefficients=(offset,),
            residual=residual,
            points=tuple(points),
        )

    # -------------------------------------------------------------- internals

    def _blocking_read(self) -> str:
        """Runs in a worker thread. ~831 ms on the target hardware."""
        return self._path.read_text()

    def _fault(self) -> SensorSample:
        return SensorSample(
            value=None,
            raw=None,
            unit="degC",
            quality="fault",
            calibration_id=self._calibration_id,
        )

    def _parse(self, text: str) -> SensorSample:
        crc = _CRC_RE.search(text)
        if crc is None or crc.group(1) != "YES":
            # A failing CRC means the bit stream was corrupted — usually a
            # marginal pull-up or too much cable. Publishing it would be worse
            # than publishing nothing.
            return self._fault()

        temp = _TEMP_RE.search(text)
        if temp is None:
            return self._fault()

        millic = int(temp.group(1))
        if millic == _POWER_ON_RESET_MILLIC:
            return self._fault()

        raw_c = millic / 1000.0
        return SensorSample(
            value=raw_c + self._offset_c,
            raw=raw_c,
            unit="degC",
            quality="ok",
            calibration_id=self._calibration_id,
        )

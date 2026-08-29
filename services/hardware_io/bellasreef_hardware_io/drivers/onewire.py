# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""DS18B20 temperature driver.

The first real driver, and the one the driver-interface timing rule was written
for. Measured on the target hardware, a single read costs **831 ms** — above the
~750 ms datasheet conversion time at 12-bit resolution — and the 1-Wire bus is
serialized, so N probes read naively cost N × 831 ms.

The event-loop half of that cost is absorbed here; the bus half is not:

* the blocking sysfs read runs in a worker thread, so the event loop keeps
  running;
* probes sharing a bus master serialize against *each other* through a
  ``threading.Lock`` owned by this module, acquired and released **inside**
  the worker thread — for the read's true lifetime, not the awaiter's. A read
  that overruns ``read_timeout_s`` cannot leave the lock released while its
  thread is still on the wire: ``asyncio.wait_for`` cancels the AWAIT, never
  the thread, so the lock has to be the thread's to hold;
* because the lock is acquired *inside* the thread, ``read_timeout_s`` now
  covers queue time as well as conversion time — a probe waiting behind
  others on the same bus master can time out, and be reported
  ``quality="fault"``, while the bus itself is healthy. The straggler thread
  is not cancelled: it performs its read once the lock frees, for a sample
  nobody receives.

That is fine at the measured 831 ms with the one probe this bus carries
today — nothing queues behind it. It stops being fine once a second probe
shares the bus master: three or more probes polling at once can queue past
the 2.0 s default on a healthy bus. ``read_timeout_s``'s semantics —
queue-inclusive, as implemented, versus conversion-only, as the name
suggests — must be re-ruled before a second probe joins this bus, not
discovered from fault samples after it does.

Read path is sysfs because that is the only interface ``w1-therm`` exposes.
This is not the forbidden sysfs GPIO.
"""

from __future__ import annotations

import asyncio
import re
import threading
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
#:
#: A ``threading.Lock``, not an ``asyncio.Lock``: serialization belongs to the
#: reader thread's domain, acquired and released inside it, so it is held for
#: the read's true lifetime rather than the awaiting coroutine's. An
#: ``asyncio.Lock`` released by ``async with`` on ``wait_for`` timeout let a
#: second probe start converting while the first's thread was still on the
#: wire — see :meth:`DS18B20._read_locked`. A plain ``threading.Lock`` also
#: has no event loop to bind to.
#:
#: ``_bus_lock`` itself is get-or-create over a plain dict and is only ever
#: called from :meth:`DS18B20.read`, which runs on the event loop — a single
#: thread, cooperatively scheduled — never from inside a worker thread. That
#: is what makes the lookup safe without its own lock: two coroutines racing
#: to create the *first* lock for a bus master would otherwise be a real
#: check-then-set race and could hand out two different ``Lock`` objects.
_BUS_LOCKS: dict[str, threading.Lock] = {}


def _bus_lock(bus_master: str) -> threading.Lock:
    lock = _BUS_LOCKS.get(bus_master)
    if lock is None:
        lock = threading.Lock()
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
        # Resolved here, on the event loop, not inside the worker thread — see
        # the get-or-create note on ``_bus_lock``. The lock itself is acquired
        # and released in ``_read_locked``, which runs in the thread.
        lock = _bus_lock(self._bus_master)
        try:
            raw_text = await asyncio.wait_for(
                asyncio.to_thread(self._read_locked, lock),
                timeout=self._read_timeout_s,
            )
        except (TimeoutError, OSError):
            # Probe unplugged, bus wedged, or conversion overran. All are
            # operating conditions on a wet installation, not exceptions.
            #
            # A timeout here only cancels this AWAIT — ``_read_locked`` keeps
            # running in its worker thread regardless, still holding ``lock``
            # until the sysfs read actually returns. That is the fix: the old
            # code held an ``asyncio.Lock`` in an ``async with`` wrapped
            # around this same ``wait_for``, so the timeout's cancellation
            # unwound the ``async with`` and released the lock immediately,
            # while the real read kept running on the bus underneath it.
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

    def _read_locked(self, lock: threading.Lock) -> str:
        """Runs in a worker thread. Holds ``lock`` for the read's true lifetime.

        Acquired and released here — inside the thread body — not around the
        awaiting coroutine. ``asyncio.wait_for`` can only cancel the AWAIT; it
        cannot stop this thread once it has started. A timed-out ``read()``
        gets its fault sample back immediately while this thread keeps the bus
        reserved until the sysfs read actually finishes; the next probe's
        thread then queues on ``lock.acquire()`` — blocking a worker thread,
        never the event loop — instead of starting a second conversion on a
        bus that is still serialized underneath it.
        """
        with lock:
            return self._blocking_read()

    def _blocking_read(self) -> str:
        """The sysfs read itself. ~831 ms on the target hardware.

        Only ever called from :meth:`_read_locked`, so it always runs with the
        bus lock held.
        """
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

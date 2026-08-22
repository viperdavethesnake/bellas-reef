# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""RP1 hardware PWM via the kernel's ``/sys/class/pwm`` interface.

The Pi's own PWM, as an :class:`~bellasreef_contracts.driver.ActuatorDriver`
behind the same contract as the PCA9685. Which silicon drives a channel is a
config choice; above hardware-io the two are indistinguishable, and the safety
rules they share live in :mod:`.dimming` so they cannot drift apart.

Not the deprecated sysfs GPIO interface
---------------------------------------
``/sys/class/gpio`` is forbidden by CLAUDE.md and absent on this board.
``/sys/class/pwm`` is a different thing entirely: the current, supported kernel
PWM ABI, and the only interface the RP1 PWM block exposes. libgpiod does not
cover PWM — it is a GPIO character-device library, and hardware PWM is not GPIO.

Measured on the target 2026-08-11: ``pwmchip0`` at ``1f0009c000.pwm`` (RP1),
``npwm`` 4, kernel 6.18.39.

The ordering is the hard part
-----------------------------
This ABI has rules that fail quietly or confusingly when broken, so they are
encoded as an ordered sequence in :meth:`PiPwmChannel.open` and enforced by the
fake in the tests — not written down in a comment and hoped for. See
:class:`SysfsWriter`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Final, Protocol

from bellasreef_contracts import ActuatorLevel, PwmLevel
from bellasreef_service import get_logger

from bellasreef_hardware_io.drivers.dimming import snap_duty

log = get_logger(__name__)

__all__ = [
    "DEFAULT_PERIOD_NS",
    "PWM_CHIP_ROOT",
    "RP1_PWM_CHANNELS",
    "PiPwmChannel",
    "RealSysfs",
    "SysfsWriter",
    "chip_device_identity",
    "chip_identity_and_facts",
    "duty_to_ns",
]

#: Default sysfs root. A path rather than a discovered device, because the
#: chip index has moved between kernel releases before — see the gpiochip note
#: in CLAUDE.md — and a caller that cares should pass its own.
PWM_CHIP_ROOT: Final = Path("/sys/class/pwm/pwmchip0")

#: 500 Hz, as period in nanoseconds: 1/500 s = 2_000_000 ns.
#:
#: The same frequency pinned for the PCA9685 and for the same bench findings:
#: the XLG-AB dimming window is 100 Hz–2 kHz once the >2 kHz spurious-triggering
#: region is excluded, and 500 Hz sits mid-window. Recorded as given — see the
#: bench boundary; this driver does not re-argue the frequency.
#:
#: Unlike the PCA9685 there is no prescaler to fight: the kernel takes a period
#: in nanoseconds and the RP1 clock divider is its problem.
DEFAULT_PERIOD_NS: Final = 2_000_000

#: How long to wait for udev to create the exported channel's directory.
#: Export returns before the attributes exist; writing into a directory that is
#: not there yet is the first thing that goes wrong on a cold boot.
_EXPORT_TIMEOUT_S: Final = 2.0
_EXPORT_POLL_S: Final = 0.01


class SysfsWriter(Protocol):
    """The filesystem operations this driver performs.

    A seam, for the same reason the PCA9685 has an ``I2CBus`` one: the ordering
    rules below are the whole substance of this driver, and a test that cannot
    observe the order of writes cannot check them. The fake in the tests
    enforces the kernel's rules; a fake that merely accepted every write would
    let all of them regress silently.
    """

    def write(self, path: Path, value: str) -> None: ...
    def read(self, path: Path) -> str: ...
    def exists(self, path: Path) -> bool: ...
    def writable(self, path: Path) -> bool: ...


class RealSysfs:
    """The actual filesystem. No cleverness on purpose."""

    def write(self, path: Path, value: str) -> None:
        path.write_text(value)

    def read(self, path: Path) -> str:
        return path.read_text().strip()

    def exists(self, path: Path) -> bool:
        return path.exists()

    def writable(self, path: Path) -> bool:
        return os.access(path, os.W_OK)


#: The RP1 PWM0 block's channel count, fixed by the silicon (CLAUDE.md,
#: verified host facts: all four channels reach a header pin on this board).
#: Distinct from ``npwm`` as read live from sysfs by capabilities.py's
#: discovery — this is the fact reported in the chip's ChipState table, not
#: a discovery bound, and the two are expected to agree on this hub.
RP1_PWM_CHANNELS: Final = 4


def chip_device_identity(chip_root: Path) -> str:
    """The pwmchip's device-tree identity, e.g. ``"1f00098000.pwm"``.

    Resolved exactly as ``capabilities.find_pwm_chip`` resolves it: the
    pwmchip index has moved between kernel releases (CLAUDE.md), so the
    stable name is what the chip's ``device`` symlink points at, never the
    ``pwmchipN`` directory name itself.
    """
    return (chip_root / "device").resolve().name


def chip_identity_and_facts(
    chip_root: Path, period_ns: int, polarity: str
) -> tuple[str, dict[str, str | int | float | bool]]:
    """This chip's instance identity and its ChipState facts, together.

    One resolve of the ``device`` symlink, not two — it is both the
    ``device`` fact and the message's ``instance`` string. Chip-level, not
    per-channel: today every RP1 channel that lights a tank shares one
    period and one polarity convention, so the channel that opens first
    describes the chip for the rest of the process's life (see app.py's
    publish-once keying).
    """
    device = chip_device_identity(chip_root)
    facts: dict[str, str | int | float | bool] = {
        "chip": chip_root.name,
        "device": device,
        "period_ns": period_ns,
        "frequency_hz": round(1e9 / period_ns, 1),
        "polarity": polarity,
        "channels": RP1_PWM_CHANNELS,
    }
    return device, facts


def duty_to_ns(duty: float, period_ns: int) -> int:
    """Duty as a nanosecond on-time, with the undefined-band rule applied.

    Snapping happens here rather than at the caller so there is no path from a
    command to a pin that skips it — the same rule, from the same constant, as
    the PCA9685 uses.
    """
    return int(snap_duty(duty) * period_ns)


class PiPwmChannel:
    """One RP1 PWM channel as an actuator.

    Implements the driver protocol structurally; see
    ``docs/contracts/driver-interface.md``.
    """

    def __init__(
        self,
        channel: int,
        actuator_id: str,
        *,
        chip_root: Path = PWM_CHIP_ROOT,
        period_ns: int = DEFAULT_PERIOD_NS,
        inverted: bool = False,
        sysfs: SysfsWriter | None = None,
        driver_id: str = "rp1-pwm",
    ) -> None:
        self._channel = channel
        self._actuator_id = actuator_id
        self._chip = chip_root
        self._period_ns = period_ns
        self._inverted = inverted
        self._sysfs: SysfsWriter = sysfs if sysfs is not None else RealSysfs()
        self._driver_id = driver_id

    # ----------------------------------------------------------- paths

    @property
    def _dir(self) -> Path:
        return self._chip / f"pwm{self._channel}"

    @property
    def driver_id(self) -> str:
        return self._driver_id

    @property
    def actuator_id(self) -> str:
        return self._actuator_id

    @property
    def chip_root(self) -> Path:
        """Which pwmchip this channel lives on. Public so app.py can build
        the chip's ChipState at the bring-up moment — see
        ``chip_identity_and_facts``."""
        return self._chip

    @property
    def period_ns(self) -> int:
        return self._period_ns

    @property
    def polarity(self) -> str:
        """The sysfs string this channel's polarity attribute was set to."""
        return "inversed" if self._inverted else "normal"

    @property
    def safe_state(self) -> ActuatorLevel:
        """Dark.

        What "dark" means electrically depends on the output stage, which is
        not this file's to reason about. ``polarity`` is the knob, set from
        measurement via ``inverted=``.
        """
        return PwmLevel(duty=0.0)

    # ----------------------------------------------------------- lifecycle

    async def open(self) -> None:
        """Bring the channel up, in the one order the kernel accepts.

        Every step here is a rule that fails badly when skipped:

        1. **Disable before touching anything**, if the channel is already
           exported. A warm restart inherits a running channel, and ``polarity``
           is only writable while the channel is disabled — the write is
           rejected outright otherwise.
        2. **Export, then wait for the directory.** ``export`` returns before
           udev has created the attributes. Writing straight after it is a race
           that loses on a cold boot and wins on a warm one, which is the worst
           kind.
        3. **Zero duty before setting period.** ``duty_cycle`` may never exceed
           ``period``. On a fresh channel ``period`` is 0, so any non-zero duty
           write is rejected; on a re-open, lowering ``period`` under a larger
           standing ``duty_cycle`` is rejected the same way.
        4. **Polarity while still disabled**, per rule 1.
        5. **Enable last**, with duty still 0 — so the channel comes up dark
           rather than at whatever the previous run left behind.
        """
        if self._sysfs.exists(self._dir):
            self._sysfs.write(self._dir / "enable", "0")
        else:
            self._sysfs.write(self._chip / "export", str(self._channel))
            await self._await_export()

        self._sysfs.write(self._dir / "duty_cycle", "0")
        self._sysfs.write(self._dir / "period", str(self._period_ns))
        self._sysfs.write(self._dir / "polarity", "inversed" if self._inverted else "normal")
        self._sysfs.write(self._dir / "duty_cycle", "0")
        self._sysfs.write(self._dir / "enable", "1")

        log.info(
            "rp1 pwm channel open",
            extra={
                "actuator_id": self._actuator_id,
                "channel": self._channel,
                "period_ns": self._period_ns,
                "polarity": "inversed" if self._inverted else "normal",
                "bench_verified": False,
            },
        )

    async def _await_export(self) -> None:
        """Wait for the channel to exist **and be writable**.

        Two separate waits, and the second is the one that bit. Export creates
        the attributes owned ``root:root 0644``; a udev rule then chgrps them to
        the ``gpio`` group. Those happen in that order and not atomically, so a
        driver that waits only for the directory writes into a window where the
        files exist and it has no permission to them:

            immediately after export:  -rw-r--r-- root root  -> EACCES
            ~300ms later:              -rw-rw-r-- root gpio  -> fine

        Measured on the target 2026-08-12. It cost a diagnosis of "the hub needs
        a udev rule" when the rule was already there and already correct — the
        service was simply faster than it.

        Polling both rather than sleeping a guess: a fixed delay is too short on
        a loaded boot and wasted on every other one.
        """
        probe = self._dir / "duty_cycle"
        waited = 0.0
        while waited < _EXPORT_TIMEOUT_S:
            if self._sysfs.exists(self._dir) and self._sysfs.writable(probe):
                return
            await asyncio.sleep(_EXPORT_POLL_S)
            waited += _EXPORT_POLL_S

        if not self._sysfs.exists(self._dir):
            raise RuntimeError(
                f"{self._dir} did not appear within {_EXPORT_TIMEOUT_S}s of export. "
                "The channel number may be beyond this chip's npwm."
            )
        raise RuntimeError(
            f"{probe} never became writable within {_EXPORT_TIMEOUT_S}s. The udev "
            "rule that chgrps exported PWM channels to the 'gpio' group has not "
            "run, or this process is not in that group."
        )

    async def close(self) -> None:
        """Dark, disabled, then unexported — in that order.

        Unexporting an enabled channel leaves the pin in a state the kernel no
        longer manages. Driving it dark first means shutdown looks like every
        other safe-state path rather than like a special case.
        """
        if not self._sysfs.exists(self._dir):
            return
        self._sysfs.write(self._dir / "duty_cycle", "0")
        self._sysfs.write(self._dir / "enable", "0")
        self._sysfs.write(self._chip / "unexport", str(self._channel))

    # ----------------------------------------------------------- driving

    async def apply(self, level: ActuatorLevel) -> None:
        if not isinstance(level, PwmLevel):
            raise TypeError(f"{self._actuator_id} is a PWM channel; got {type(level).__name__}")
        self._sysfs.write(self._dir / "duty_cycle", str(duty_to_ns(level.duty, self._period_ns)))

    async def drive_safe(self) -> None:
        """Zero on-time, without consulting anything.

        Duty rather than ``enable``: disabling releases the pin to whatever the
        pad default is, and this must land somewhere known. It is also called
        precisely when the spine, the engine and the database are gone, so it
        touches none of them.
        """
        self._sysfs.write(self._dir / "duty_cycle", "0")

    async def read_back(self) -> ActuatorLevel | None:
        """What the kernel currently has, which is more than the PCA9685 offers.

        Still not a measurement of light — it is the kernel's own record of what
        it was told — but unlike an I²C register echo it survives a process
        restart and can disagree with what this object believes it wrote. That
        makes it worth reporting.
        """
        try:
            duty_ns = int(self._sysfs.read(self._dir / "duty_cycle"))
            period_ns = int(self._sysfs.read(self._dir / "period"))
        except (OSError, ValueError):
            return None
        if period_ns <= 0:
            return None
        return PwmLevel(duty=min(duty_ns / period_ns, 1.0))

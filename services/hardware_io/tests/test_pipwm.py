# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""RP1 hardware PWM driver, against a fake that enforces the kernel's rules.

The substance of this driver is the *order* it writes sysfs attributes in, so a
fake that accepted every write in any order would test nothing that matters.
:class:`FakeSysfs` rejects what the kernel rejects:

* ``duty_cycle`` may never exceed ``period`` — which on a fresh channel is 0,
  so any non-zero duty before a period is an error;
* ``polarity`` is writable only while the channel is disabled;
* attributes do not exist until the channel is exported.

Same shape as the PCA9685 fake, which enforces the prescale-under-SLEEP rule and
would have caught an ordering bug the driver never had. That is the pattern: the
fake models the trap, not the happy path.

The one test that needs real hardware is marked and skipped elsewhere.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from bellasreef_contracts import BinaryLevel, PwmLevel
from bellasreef_hardware_io.drivers.dimming import MIN_USABLE_DUTY
from bellasreef_hardware_io.drivers.pipwm import (
    DEFAULT_PERIOD_NS,
    PiPwmChannel,
    duty_to_ns,
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class SysfsError(OSError):
    """What the kernel would have returned."""


class FakeSysfs:
    """A /sys/class/pwm that says no in the same places the real one does."""

    def __init__(self, *, npwm: int = 4, export_delay: int = 0, chgrp_delay: int = 0) -> None:
        self.values: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.exported: set[int] = set()
        self._npwm = npwm
        #: Number of `exists` checks to fail before the directory "appears",
        #: modelling udev latency after export.
        self._export_delay = export_delay
        self._checks = 0
        #: How many writability checks fail before udev "runs".
        self._chgrp_delay = chgrp_delay
        self._perm_checks = 0

    def _key(self, path: Path) -> str:
        return str(path)

    def writable(self, path: Path) -> bool:
        """Writable only once udev has caught up.

        The real chip creates the attributes root:root 0644 on export and a
        udev rule chgrps them to `gpio` a moment later. A fake that reported
        everything writable the instant it existed could not catch a driver
        that races that window — which is exactly the bug this models.
        """
        channel_dir = path.parent
        if not self.exists(channel_dir):
            return False
        self._perm_checks += 1
        return self._perm_checks > self._chgrp_delay

    def exists(self, path: Path) -> bool:
        name = path.name
        if name.startswith("pwm") and name != "pwmchip0":
            channel = int(name.removeprefix("pwm"))
            if channel not in self.exported:
                return False
            self._checks += 1
            return self._checks > self._export_delay
        return True

    def read(self, path: Path) -> str:
        if self._key(path) not in self.values:
            raise SysfsError(f"no such attribute: {path}")
        return self.values[self._key(path)]

    def write(self, path: Path, value: str) -> None:
        name = path.name

        if name == "export":
            channel = int(value)
            if channel >= self._npwm:
                raise SysfsError(f"channel {channel} beyond npwm={self._npwm}")
            self.exported.add(channel)
            self._checks = 0
            self.writes.append((name, value))
            return

        if name == "unexport":
            self.exported.discard(int(value))
            self.writes.append((name, value))
            return

        channel_dir = path.parent
        if not self.exists(channel_dir):
            raise SysfsError(f"{channel_dir} not exported")

        if name == "duty_cycle":
            period = int(self.values.get(str(channel_dir / "period"), "0"))
            if int(value) > period:
                raise SysfsError(
                    f"duty_cycle {value} exceeds period {period} — the kernel "
                    "rejects this, and on a fresh channel period is 0"
                )

        if name == "period":
            duty = int(self.values.get(str(channel_dir / "duty_cycle"), "0"))
            if int(value) < duty:
                raise SysfsError(
                    f"period {value} below standing duty_cycle {duty}; zero the "
                    "duty before lowering the period"
                )

        if name == "polarity" and self.values.get(str(channel_dir / "enable")) == "1":
            raise SysfsError("polarity is writable only while the channel is disabled")

        self.values[self._key(path)] = value
        self.writes.append((name, value))

    def sequence(self) -> list[str]:
        return [name for name, _ in self.writes]


def _channel(sysfs: FakeSysfs, **kw: Any) -> PiPwmChannel:
    return PiPwmChannel(0, "led-blue", chip_root=Path("/sys/class/pwm/pwmchip0"), sysfs=sysfs, **kw)


# ------------------------------------------------------------- duty mapping


@pytest.mark.parametrize("duty", [0.001, 0.01, 0.05, 0.079])
def test_anything_under_eight_percent_snaps_to_zero(duty: float) -> None:
    """The same rule as the PCA9685, from the same constant.

    Both PWM sources share :mod:`.dimming` precisely so this cannot be true of
    one driver and not the other — a channel's safety behaviour must not depend
    on which silicon happens to be driving it.
    """
    assert duty_to_ns(duty, DEFAULT_PERIOD_NS) == 0


def test_eight_percent_is_honoured() -> None:
    assert duty_to_ns(MIN_USABLE_DUTY, DEFAULT_PERIOD_NS) == int(
        MIN_USABLE_DUTY * DEFAULT_PERIOD_NS
    )


def test_a_ramp_never_lands_inside_the_undefined_band() -> None:
    """Dawn and dusk are the daily path through this band, not an edge case."""
    floor = int(MIN_USABLE_DUTY * DEFAULT_PERIOD_NS)
    for step in range(101):
        ns = duty_to_ns(step / 100, DEFAULT_PERIOD_NS)
        assert ns == 0 or ns >= floor


def test_the_period_is_five_hundred_hertz() -> None:
    """Same frequency as the PCA9685, recorded as given from bench findings."""
    assert DEFAULT_PERIOD_NS == 2_000_000
    assert 1e9 / DEFAULT_PERIOD_NS == 500


# ------------------------------------------------------------ open ordering


def test_open_writes_the_attributes_in_the_order_the_kernel_accepts() -> None:
    """The whole substance of this driver.

    The fake rejects each violation independently, so this passing means the
    order is right rather than that the assertions were written to match
    whatever the code happened to do.
    """

    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs()
        await _channel(sysfs).open()
        return sysfs

    sysfs = run(scenario)
    assert sysfs.sequence() == [
        "export",
        "duty_cycle",  # zero first: period is 0, any other value is rejected
        "period",
        "polarity",  # only legal while still disabled
        "duty_cycle",
        "enable",  # last, and dark
    ]


def test_the_channel_comes_up_dark() -> None:
    """Enable happens with duty still zero.

    A warm restart must not resume at whatever the previous run was doing while
    nobody is supervising it yet.
    """

    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs()
        await _channel(sysfs).open()
        return sysfs

    sysfs = run(scenario)
    order = sysfs.sequence()
    assert order.index("enable") > order.index("duty_cycle")
    assert sysfs.values["/sys/class/pwm/pwmchip0/pwm0/duty_cycle"] == "0"
    assert sysfs.values["/sys/class/pwm/pwmchip0/pwm0/enable"] == "1"


def test_reopening_a_running_channel_disables_before_touching_polarity() -> None:
    """A warm restart inherits an enabled channel, where polarity is read-only."""

    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs()
        await _channel(sysfs).open()
        sysfs.writes.clear()
        await _channel(sysfs).open()  # second process, same channel
        return sysfs

    sysfs = run(scenario)
    order = sysfs.sequence()
    assert order[0] == "enable"
    assert sysfs.writes[0] == ("enable", "0")
    assert order.index("enable") < order.index("polarity")
    assert "export" not in order, "already exported; a second export is an error"


def test_export_latency_is_waited_out_not_assumed() -> None:
    """`export` returns before udev creates the attributes.

    A driver that writes immediately races, and wins on a warm boot and loses on
    a cold one — which is how it reaches production.
    """

    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs(export_delay=5)
        await _channel(sysfs).open()
        return sysfs

    sysfs = run(scenario)
    assert sysfs.sequence()[0] == "export"
    assert sysfs.values["/sys/class/pwm/pwmchip0/pwm0/enable"] == "1"


def test_the_udev_permission_window_is_waited_out() -> None:
    """Export creates the attributes root:root; udev chgrps them a moment later.

    The driver used to wait for the directory and then write immediately, into
    a window where the files exist and it has no permission to them. On the
    real hub that produced EACCES on duty_cycle and a diagnosis of "we need a
    udev rule" — when the rule was already present and correct, and the service
    was simply faster than it.
    """

    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs(chgrp_delay=4)
        await _channel(sysfs).open()
        return sysfs

    sysfs = run(scenario)
    assert sysfs.values["/sys/class/pwm/pwmchip0/pwm0/enable"] == "1"


def test_a_channel_that_never_becomes_writable_names_the_reason() -> None:
    """Distinct from "never appeared" — different cause, different fix."""

    async def scenario() -> None:
        await _channel(FakeSysfs(chgrp_delay=10_000)).open()

    with pytest.raises(RuntimeError, match="never became writable"):
        run(scenario)


def test_a_channel_that_never_appears_raises_rather_than_hanging() -> None:
    async def scenario() -> None:
        sysfs = FakeSysfs(export_delay=10_000)
        channel = _channel(sysfs)
        await channel.open()

    with pytest.raises(RuntimeError, match="did not appear"):
        run(scenario)


def test_polarity_follows_the_inverted_flag() -> None:
    """Set from measurement, not from argument — see the bench boundary."""

    async def scenario() -> tuple[str, str]:
        normal = FakeSysfs()
        await _channel(normal).open()
        inverted = FakeSysfs()
        await _channel(inverted, inverted=True).open()
        return (
            normal.values["/sys/class/pwm/pwmchip0/pwm0/polarity"],
            inverted.values["/sys/class/pwm/pwmchip0/pwm0/polarity"],
        )

    assert run(scenario) == ("normal", "inversed")


# ----------------------------------------------------------------- driving


def test_apply_writes_nanoseconds_of_on_time() -> None:
    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs()
        channel = _channel(sysfs)
        await channel.open()
        await channel.apply(PwmLevel(duty=0.5))
        return sysfs

    sysfs = run(scenario)
    assert sysfs.values["/sys/class/pwm/pwmchip0/pwm0/duty_cycle"] == str(DEFAULT_PERIOD_NS // 2)


def test_drive_safe_zeroes_duty_rather_than_disabling() -> None:
    """Disabling releases the pin to its pad default, which is not a known state."""

    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs()
        channel = _channel(sysfs)
        await channel.open()
        await channel.apply(PwmLevel(duty=0.9))
        sysfs.writes.clear()
        await channel.drive_safe()
        return sysfs

    sysfs = run(scenario)
    assert sysfs.sequence() == ["duty_cycle"]
    assert sysfs.values["/sys/class/pwm/pwmchip0/pwm0/duty_cycle"] == "0"
    assert sysfs.values["/sys/class/pwm/pwmchip0/pwm0/enable"] == "1"


def test_close_goes_dark_before_unexporting() -> None:
    async def scenario() -> FakeSysfs:
        sysfs = FakeSysfs()
        channel = _channel(sysfs)
        await channel.open()
        sysfs.writes.clear()
        await channel.close()
        return sysfs

    sysfs = run(scenario)
    assert sysfs.sequence() == ["duty_cycle", "enable", "unexport"]


def test_a_binary_level_is_refused() -> None:
    async def scenario() -> None:
        sysfs = FakeSysfs()
        channel = _channel(sysfs)
        await channel.open()
        await channel.apply(BinaryLevel(on=True))

    with pytest.raises(TypeError):
        run(scenario)


def test_read_back_reports_the_kernel_s_own_record() -> None:
    """Not a measurement of light, but it survives a restart and can disagree."""

    async def scenario() -> Any:
        sysfs = FakeSysfs()
        channel = _channel(sysfs)
        await channel.open()
        await channel.apply(PwmLevel(duty=0.25))
        return await channel.read_back()

    assert run(scenario) == PwmLevel(duty=0.25)


def test_read_back_is_none_when_the_channel_is_not_there() -> None:
    async def scenario() -> Any:
        return await _channel(FakeSysfs()).read_back()

    assert run(scenario) is None


# ------------------------------------------------------------ off the event loop
#
# Every sysfs write here is a blocking syscall (`path.write_text`), reached
# straight from `async def apply`/`drive_safe` with no executor hop. A stalled
# write — a wedged pwmchip, a slow filesystem — stalls the whole process,
# including the heartbeat watcher in safety.py. Same shape as the PCA9685's
# I2C bus, and the same fix: offload to a thread. Mirrors onewire.py's
# `test_slow_probe_does_not_stall_the_event_loop`.


class BlockingSysfs(FakeSysfs):
    """A sysfs whose writes take real wall-clock time once armed.

    Armed only after ``open()`` so the setup sequence itself stays instant —
    what's under test is the command path (``apply``/``drive_safe``), not
    channel bring-up. ``threading.Event.wait(timeout=...)`` always times out
    (nothing ever sets it), which reads as "blocked until released or the
    clock runs out" — a wedged pwmchip, not a scripted delay.
    """

    def __init__(self, block_s: float = 0.3, **kw: Any) -> None:
        super().__init__(**kw)
        self._never = threading.Event()
        self._block_s = block_s
        self.blocking = False

    def write(self, path: Path, value: str) -> None:
        if self.blocking:
            self._never.wait(timeout=self._block_s)
        super().write(path, value)


def test_a_blocked_apply_write_does_not_stall_the_event_loop() -> None:
    """If ``apply()`` ran the write straight on the loop, the ticker below
    would barely advance for the ~300 ms the write takes."""
    sysfs = BlockingSysfs()
    channel = _channel(sysfs)
    ticks = 0

    async def scenario() -> None:
        nonlocal ticks
        await channel.open()
        sysfs.blocking = True

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        await channel.apply(PwmLevel(duty=0.5))
        t.cancel()

    run(scenario)
    # ~30 ticks expected in 300 ms. A blocked loop would yield ~0.
    assert ticks > 10, f"event loop was stalled by the write (only {ticks} ticks)"


def test_a_blocked_drive_safe_write_does_not_stall_the_event_loop() -> None:
    """Doubly so for ``drive_safe`` — it is what the heartbeat watcher calls."""
    sysfs = BlockingSysfs()
    channel = _channel(sysfs)
    ticks = 0

    async def scenario() -> None:
        nonlocal ticks
        await channel.open()
        sysfs.blocking = True

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        await channel.drive_safe()
        t.cancel()

    run(scenario)
    assert ticks > 10, f"event loop was stalled by drive_safe (only {ticks} ticks)"


# ------------------------------------------------------------ registration


def test_the_registration_is_authoritative_light_with_the_full_triple() -> None:
    """Asserted through the real supervisor, which refuses anything less.

    Identical to the PCA9685's, from the same shared helper: a channel's safety
    contract must not change because the wiring moved to different silicon.
    """
    from bellasreef_hardware_io.drivers.dimming import light_registration
    from bellasreef_hardware_io.safety import InterlockSupervisor

    channel = _channel(FakeSysfs())
    reg = light_registration(actuator_id="led-blue", driver_id="rp1-pwm")

    assert reg.control_authority == "authoritative"
    assert reg.role == "light"
    assert reg.safe_state == PwmLevel(duty=0.0)

    supervisor = InterlockSupervisor(on_event=_ignore)
    supervisor.register(reg, channel)
    assert supervisor.registration_of("led-blue").actuator_id == "led-blue"


async def _ignore(event: object) -> None:
    return None


# ------------------------------------------------------- real hardware only

_PI_PWM = "BELLASREEF_TEST_PWM_CHANNEL"

#: `hardware` is declared in pyproject: "requires real I2C/GPIO/1-wire on the
#: Pi (never runs in CI)". The marker is what tells the skip-policing hook in
#: conftest.py that this absence is structural rather than an environment
#: somebody forgot to provide.
pytestmark = pytest.mark.hardware

requires_pwm_hardware = pytest.mark.skipif(
    not os.environ.get(_PI_PWM),
    reason=(
        f"{_PI_PWM} not set; this drives a real RP1 PWM pin and only runs on the Pi "
        "with an overlay loaded and David present"
    ),
)


@requires_pwm_hardware
def test_a_real_channel_opens_and_reads_back_what_it_was_told() -> None:
    """The only test here that touches silicon.

    Deliberately asserts nothing about volts or light — that is David's meter,
    per the bench boundary. What it proves is narrower and still worth proving:
    the export/period/polarity/enable sequence is accepted by this kernel, and
    the channel reads back the duty it was given.

    Set BELLASREEF_TEST_PWM_CHANNEL to the channel number to run it.
    """

    async def scenario() -> Any:
        channel = PiPwmChannel(int(os.environ[_PI_PWM]), "bench-pwm")
        await channel.open()
        try:
            await channel.apply(PwmLevel(duty=0.5))
            return await channel.read_back()
        finally:
            await channel.drive_safe()
            await channel.close()

    level = run(scenario)
    assert isinstance(level, PwmLevel)
    assert abs(level.duty - 0.5) < 0.01

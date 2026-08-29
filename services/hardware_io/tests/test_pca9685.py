# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""PCA9685 driver, against a fake bus.

Nothing here has driven a real light, and no test in this file claims
otherwise. What these do is pin the decisions the bench *checked* on 2026-08-15
— the polarity, the prescaler, the duty mapping — so that when the meter
disagrees, exactly one constant changes and the test that has to move says so.
The polarity and oscillator assertions below are now measured values rather
than expectations; Stage 2, the same points through the stack, is still open.

The fake models the two chip behaviours that actually bite:

* registers read back what was written, so a readback assertion is meaningful;
* **PRE_SCALE is silently discarded unless SLEEP is set**, which is the real
  silicon's most treacherous property. A fake that accepted the write at any
  time would let the driver's ordering bug pass forever.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from bellasreef_contracts import BinaryLevel, PwmLevel
from bellasreef_hardware_io.drivers.dimming import MIN_USABLE_DUTY
from bellasreef_hardware_io.drivers.pca9685 import (
    INVRT_ON,
    OPEN_DRAIN,
    PCA9685_OSC_HZ,
    PCA9685_PRE_SCALE,
    Pca9685Channel,
    Pca9685Device,
    duty_to_counts,
)

_MODE1 = 0x00
_MODE2 = 0x01
_LED0_ON_L = 0x06
_ALL_LED_ON_L = 0xFA
_PRE_SCALE = 0xFE
_SLEEP = 0x10
_OUTDRV = 0x04
_INVRT = 0x10
_FULL = 0x10


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class FakeBus:
    """A PCA9685 that powers up the way the real one on this bench did.

    MODE1 0x11, MODE2 0x04, PRE_SCALE 0x1e — measured 2026-08-09 and recorded in
    CLAUDE.md. Starting from the real power-on state rather than zeros is the
    difference between testing the driver and testing an idealisation of it.
    """

    def __init__(self) -> None:
        self.registers: dict[int, int] = {_MODE1: 0x11, _MODE2: 0x04, _PRE_SCALE: 0x1E}
        self.blocks: list[tuple[int, list[int]]] = []
        #: Writes the real chip would have thrown away.
        self.discarded_prescale_writes = 0

    def read_byte_data(self, address: int, register: int) -> int:
        return self.registers.get(register, 0)

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        if register == _PRE_SCALE and not self.registers.get(_MODE1, 0) & _SLEEP:
            # The prescaler is latched off the sleeping oscillator. On real
            # silicon this write lands nowhere and reports no error.
            self.discarded_prescale_writes += 1
            return
        self.registers[register] = value

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        self.blocks.append((register, list(data)))


def _device() -> tuple[Pca9685Device, FakeBus]:
    bus = FakeBus()
    return Pca9685Device(bus), bus


# ------------------------------------------------------------ the duty mapping


def test_zero_duty_is_hard_off_not_a_zero_count() -> None:
    """The full-off bit, not a counted edge.

    A 0-count off time still emits a one-tick sliver on some silicon, and the
    declared safe state cannot be "almost off".
    """
    on, off = duty_to_counts(0.0)
    assert on == 0
    assert off == _FULL << 8


@pytest.mark.parametrize("duty", [0.001, 0.01, 0.05, 0.079, 0.0799999])
def test_anything_under_eight_percent_snaps_to_zero(duty: float) -> None:
    """The session-4 ruling, and the reason it is not "clamp up to 0.08".

    The XLG output is undefined below 8%: it may flicker, sit dark, or go to
    full. Snapping down cannot leave a channel emitting at a duty the hardware
    refuses to define. Clamping up would light the tank at dawn when the
    schedule asked for almost nothing.
    """
    assert duty_to_counts(duty) == duty_to_counts(0.0)


def test_exactly_eight_percent_is_honoured() -> None:
    """The boundary is usable, not undefined — snapping it would lose the floor."""
    on, off = duty_to_counts(MIN_USABLE_DUTY)
    assert (on, off) != duty_to_counts(0.0)
    assert off == round(MIN_USABLE_DUTY * 4096)


def test_a_dawn_ramp_crosses_the_band_and_never_lands_inside_it() -> None:
    """The daily path, not an edge case.

    A diurnal ramp crosses the undefined band twice a day, so this walks one
    and asserts that every step is either hard off or at/above the floor.
    Nothing may land between, in either direction.
    """
    for step in range(0, 101):
        duty = step / 100
        on, off = duty_to_counts(duty)
        if (on, off) == duty_to_counts(0.0):
            continue
        counts = (_FULL << 8) if on else off
        assert counts >= round(MIN_USABLE_DUTY * 4096) or on, (
            f"duty {duty} produced counts inside the undefined band"
        )


def test_full_duty_uses_the_full_on_bit() -> None:
    on, off = duty_to_counts(1.0)
    assert on == _FULL << 8
    assert off == 0


# --------------------------------------------------------------- chip setup


def test_the_prescaler_is_written_while_asleep_and_read_back() -> None:
    """Both halves matter, and the fake proves each independently.

    Writing PRE_SCALE on a running chip is discarded silently, which presents as
    "the frequency setting is ignored". The readback is what turns that from a
    mystery into an exception.
    """

    async def scenario() -> FakeBus:
        device, bus = _device()
        await device.initialise()
        return bus

    bus = run(scenario)
    assert bus.registers[_PRE_SCALE] == PCA9685_PRE_SCALE
    assert bus.discarded_prescale_writes == 0, (
        "the driver wrote PRE_SCALE while the chip was awake; the real chip "
        "would have thrown that write away without complaining"
    )


def test_a_prescaler_that_does_not_stick_is_an_error_not_a_shrug() -> None:
    """The failure this readback exists to catch."""

    class Stubborn(FakeBus):
        def write_byte_data(self, address: int, register: int, value: int) -> None:
            if register == _PRE_SCALE:
                return  # accepts and discards, exactly like a running chip
            super().write_byte_data(address, register, value)

    async def scenario() -> None:
        device = Pca9685Device(Stubborn())
        await device.initialise()

    with pytest.raises(RuntimeError, match="PRE_SCALE readback"):
        run(scenario)


def test_the_prescaler_is_five_hundred_hertz_on_the_measured_oscillator() -> None:
    """30 (≈196.9 Hz) is inside the XLG window but was never a chosen value.

    The pinned frequency is 500 Hz: mid-window, clear of the flicker floor and
    clear of the >2 kHz spurious-triggering region. The prescaler that gets
    there is computed from the *measured* oscillator, not the datasheet's.
    """
    assert PCA9685_PRE_SCALE == 12
    actual_hz = PCA9685_OSC_HZ / (4096 * (PCA9685_PRE_SCALE + 1))
    assert 100 < actual_hz < 2000, "outside the usable XLG-AB dimming window"
    assert abs(actual_hz - 500) / 500 < 0.02, f"{actual_hz:.1f} Hz is not ~500 Hz"


def test_the_datasheet_oscillator_is_why_eleven_was_wrong() -> None:
    """Why this constant moved from 11 to 12, as arithmetic rather than prose.

    The datasheet's 25 MHz makes 11 look correct — ≈508.6 Hz, comfortably
    mid-window. The chip on this bench measured 544.7/544.8 Hz at that same
    prescaler on 2026-08-15, and 1307 Hz at PRE_SCALE 4: a constant ratio,
    ~7.1 % fast, implying ≈26.77 MHz. Asking a 25 MHz formula for 500 Hz on
    this chip silently returns 545.
    """
    assert round(PCA9685_OSC_HZ / 25_000_000, 3) == 1.071

    datasheet_at_eleven = 25_000_000 / (4096 * 12)
    measured_at_eleven = PCA9685_OSC_HZ / (4096 * 12)
    assert round(datasheet_at_eleven, 1) == 508.6
    assert round(measured_at_eleven) == 545, "the bench read 544.7/544.8 Hz here"


# ------------------------------------------------------- polarity, the bench knob


def test_output_is_totem_pole_with_inversion_off_as_measured() -> None:
    """MODE2 ``0x04`` — OUTDRV set, INVRT clear — as measured on 2026-08-15.

    Totem-pole is the 2026-08-11 ruling: the PCA9685 drives an external N-FET
    gate and the 10 V dim line lives on the FET drain, not on LEDn. Recorded as
    ruled — this test checks the registers match the decision and does not argue
    the electronics.

    INVRT clear is Stage 1, not an expectation: with MODE2 ``0x04`` the meter at
    the LEDn pin read 0 V at duty 0 and 3.307 V at duty 1.0, linear between. The
    earlier ``INVRT_ON = True`` would have driven this channel to full output on
    its declared safe state. If a later output stage measures inverted, one
    constant flips and this assertion flips with it. One constant, one test, one
    place.

    Supersedes an open-drain configuration written for a withdrawn bench design
    that put a 10 V pull-up on LEDn directly, which is out of spec at a 5.5 V
    absolute maximum.
    """

    async def scenario() -> FakeBus:
        device, bus = _device()
        await device.initialise()
        return bus

    bus = run(scenario)
    mode2 = bus.registers[_MODE2]

    assert OPEN_DRAIN is False
    assert mode2 & _OUTDRV, "OUTDRV clear — the chip is open-drain, not driving a gate"
    assert INVRT_ON is False
    assert not mode2 & _INVRT, "INVRT set — duty 0.0 would drive the channel to full output"
    assert mode2 == _OUTDRV, f"MODE2 is {mode2:#04x}, the bench configured 0x04"


def test_a_chip_left_inverted_by_the_old_driver_comes_back_to_the_measured_state() -> None:
    """The upgrade path this constant flip creates.

    A hub that ran the earlier ``INVRT_ON = True`` driver leaves the chip at
    MODE2 ``0x14`` (OUTDRV | INVRT). The next start must clear INVRT, not just
    leave a clear bit clear: the power-on fake above never has it set, so this
    is the one case that proves ``initialise()`` actively writes the measured
    polarity rather than inheriting whatever the silicon was left with.
    """

    async def scenario() -> FakeBus:
        bus = FakeBus()
        bus.registers[_MODE2] = _OUTDRV | _INVRT  # 0x14, as the old driver left it
        device = Pca9685Device(bus)
        await device.initialise()
        return bus

    bus = run(scenario)
    mode2 = bus.registers[_MODE2]
    assert not mode2 & _INVRT, "INVRT survived from the previous driver — duty 0.0 is full output"
    assert mode2 == _OUTDRV, f"MODE2 is {mode2:#04x}, expected 0x04"


def test_every_channel_is_driven_off_before_the_frequency_changes() -> None:
    """Initialise runs against a chip whose previous state is unknown.

    A crashed process or a warm restart leaves outputs wherever they were, and
    changing the PWM frequency underneath live channels is not a thing to do
    over a tank.
    """

    async def scenario() -> FakeBus:
        device, bus = _device()
        await device.initialise()
        return bus

    bus = run(scenario)
    first_register, first_payload = bus.blocks[0]
    assert first_register == _ALL_LED_ON_L
    assert first_payload == [0x00, 0x00, 0x00, _FULL]


# ------------------------------------------------------------- the channel


def test_a_channel_writes_its_own_register_block() -> None:
    async def scenario() -> FakeBus:
        device, bus = _device()
        channel = Pca9685Channel(device, 3, "led-blue")
        await channel.apply(PwmLevel(duty=0.5))
        return bus

    bus = run(scenario)
    register, payload = bus.blocks[-1]
    assert register == _LED0_ON_L + 4 * 3
    off = payload[2] | (payload[3] << 8)
    assert off == round(0.5 * 4096)


def test_the_declared_safe_state_is_dark() -> None:
    device, _ = _device()
    channel = Pca9685Channel(device, 0, "led-blue")
    assert channel.safe_state == PwmLevel(duty=0.0)


def test_drive_safe_writes_hard_off() -> None:
    async def scenario() -> FakeBus:
        device, bus = _device()
        channel = Pca9685Channel(device, 7, "led-white")
        await channel.apply(PwmLevel(duty=0.9))
        await channel.drive_safe()
        return bus

    bus = run(scenario)
    register, payload = bus.blocks[-1]
    assert register == _LED0_ON_L + 4 * 7
    assert payload == [0x00, 0x00, 0x00, _FULL]


def test_a_binary_level_is_refused() -> None:
    """A PWM channel told to be a relay is a wiring error, not a rounding one."""

    async def scenario() -> None:
        device, _ = _device()
        channel = Pca9685Channel(device, 0, "led-blue")
        await channel.apply(BinaryLevel(on=True))

    with pytest.raises(TypeError):
        run(scenario)


def test_read_back_is_none_rather_than_an_echo() -> None:
    """The registers echo what was written and prove nothing about the light.

    Returning that as a confirmed level would turn a commanded value into a
    measured one, and losing that distinction is what makes a stuck output
    undetectable.
    """

    async def scenario() -> Any:
        device, _ = _device()
        return await Pca9685Channel(device, 0, "led-blue").read_back()

    assert run(scenario) is None


def test_a_channel_outside_the_chip_is_refused() -> None:
    device, _ = _device()
    with pytest.raises(ValueError, match="out of range"):
        device.write_channel(16, 0.5)


# ------------------------------------------------------------- registration


def test_the_registration_is_authoritative_light_with_the_full_triple() -> None:
    """R1, asserted through the supervisor rather than by reading fields back.

    Checking the ActuatorRegistration's own attributes would only prove the
    constructor took them. Handing it to the real InterlockSupervisor proves the
    safety framework accepts it — and that supervisor refuses anything not
    authoritative (device-classes.md §3) and refuses an authoritative device
    missing any leg of the triple.
    """
    from bellasreef_hardware_io.drivers.pca9685 import registration
    from bellasreef_hardware_io.safety import InterlockSupervisor

    device, _ = _device()
    channel = Pca9685Channel(device, 0, "led-blue")
    reg = registration(channel)

    assert reg.control_authority == "authoritative"
    assert reg.role == "light"
    assert reg.actuator_class == "pwm"
    assert reg.safe_state == PwmLevel(duty=0.0)

    supervisor = InterlockSupervisor(on_event=_ignore)
    supervisor.register(reg, channel)
    assert supervisor.registration_of("led-blue").actuator_id == "led-blue"


def test_max_runtime_is_a_runaway_bound_not_a_photoperiod() -> None:
    """A reef light runs 10-12 hours a day.

    A cap anywhere near that trips on an ordinary Tuesday, and a safety limit
    that cries wolf daily is one the operator learns to ignore — which is worse
    than not having it.
    """
    from bellasreef_hardware_io.drivers.pca9685 import registration

    device, _ = _device()
    reg = registration(Pca9685Channel(device, 0, "led-blue"))
    assert reg.max_runtime_s is not None
    assert reg.max_runtime_s > 14 * 3600, "would trip on a normal photoperiod"
    assert reg.max_runtime_s < 24 * 3600, "a bound of a day or more bounds nothing"


async def _ignore(event: object) -> None:
    return None


# --------------------------------------------------------------- lifecycle
#
# Found on the bench 2026-08-17, Stage 2: every voltage row matched the CLI
# and the frequency did not — the chip was still on the PRE_SCALE Stage 1 had
# left it with. ``initialise()`` existed, was tested, and had no production
# caller. hardware-io opens actuators by duck-typing ``driver.open()``
# (``app.py``), which the RP1 channel has and this one did not, so a
# PCA9685 channel was driven on whatever the silicon happened to hold.


def test_opening_a_channel_initialises_its_chip() -> None:
    """``open()`` is the lifecycle hook the app calls; it must configure the chip."""

    async def scenario() -> FakeBus:
        device, bus = _device()
        channel = Pca9685Channel(device, 0, "pca9685-0")
        await channel.open()
        return bus

    bus = run(scenario)
    assert bus.registers[_PRE_SCALE] == PCA9685_PRE_SCALE
    assert not bus.registers[_MODE1] & _SLEEP
    assert bus.discarded_prescale_writes == 0


def test_sixteen_channels_share_one_chip_and_initialise_it_once() -> None:
    """Sixteen ``open()`` calls on one chip must not sleep/restart it sixteen
    times — the first channel up would be blacked out by every later one."""

    async def scenario() -> FakeBus:
        device, bus = _device()
        for ch in range(16):
            await Pca9685Channel(device, ch, f"pca9685-{ch}").open()
        return bus

    bus = run(scenario)
    all_off_writes = [b for b in bus.blocks if b[0] == _ALL_LED_ON_L]
    assert len(all_off_writes) == 1


# ------------------------------------------------------------ off the event loop
#
# Every smbus2 transaction is a blocking syscall. Reached straight from `async
# def apply`/`drive_safe`/`initialise` with no executor hop, a wedged or
# clock-stretching bus stalls the whole process — including the heartbeat
# watcher in safety.py whose entire job is to notice a stall. The DS18B20
# driver (onewire.py) already offloads its sysfs read the same way; these
# tests are that file's `test_slow_probe_does_not_stall_the_event_loop` and
# `test_probes_on_one_bus_serialise_against_each_other`, aimed at the I2C bus.


class BlockingBus(FakeBus):
    """A chip whose write takes real wall-clock time.

    ``threading.Event.wait(timeout=...)`` rather than ``time.sleep`` — nobody
    ever sets the event, so this always times out, but it reads as "blocked
    until released or the clock runs out", which is what a wedged or
    clock-stretching bus actually looks like.
    """

    def __init__(self, block_s: float = 0.3) -> None:
        super().__init__()
        self._never = threading.Event()
        self._block_s = block_s

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        self._never.wait(timeout=self._block_s)
        super().write_i2c_block_data(address, register, data)


def test_a_blocked_write_does_not_stall_the_event_loop() -> None:
    """The whole point of offloading to a thread.

    If ``apply()`` ran the blocking write straight on the loop, the ticker
    below would barely advance for the ~300 ms the write takes.
    """
    bus = BlockingBus()
    device = Pca9685Device(bus)
    channel = Pca9685Channel(device, 0, "led-blue")
    ticks = 0

    async def scenario() -> None:
        nonlocal ticks

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


def test_a_blocked_drive_safe_does_not_stall_the_event_loop() -> None:
    """Doubly so for ``drive_safe`` — it is what the heartbeat watcher calls."""
    bus = BlockingBus()
    device = Pca9685Device(bus)
    channel = Pca9685Channel(device, 0, "led-blue")
    ticks = 0

    async def scenario() -> None:
        nonlocal ticks

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


class TrackedBus(FakeBus):
    """Records whether two writes to this chip were ever in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.overlap = 0

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        self.active += 1
        if self.active > 1:
            self.overlap += 1
        time.sleep(0.05)
        self.active -= 1
        super().write_i2c_block_data(address, register, data)


def test_two_channels_on_one_chip_serialise_their_writes() -> None:
    """One shared chip, sixteen channels: offloaded writes must still queue.

    Two ``to_thread`` calls with no lock between them would let one channel's
    write land mid-way through another's — on real silicon that is a
    corrupted register block, not just a reordering.
    """
    bus = TrackedBus()
    device = Pca9685Device(bus)
    a = Pca9685Channel(device, 0, "led-a")
    b = Pca9685Channel(device, 1, "led-b")

    async def scenario() -> None:
        await asyncio.gather(a.apply(PwmLevel(duty=0.5)), b.apply(PwmLevel(duty=0.5)))

    run(scenario)
    assert bus.overlap == 0, "two channel writes on the same chip overlapped"


class ReleasableBus(FakeBus):
    """A chip whose write blocks until the test explicitly releases it.

    Distinct from :class:`BlockingBus`'s timeout: this one needs precise
    control over when the in-flight write actually lands, to prove a
    cancelled caller cannot make the lock available before that happens.
    Also tracks overlap, the same way :class:`TrackedBus` does — a second
    write starting while this one is still blocked is exactly the bug.
    """

    def __init__(self) -> None:
        super().__init__()
        self.write_started = threading.Event()
        self.active = 0
        self.overlap = 0
        self._release = threading.Event()

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        self.active += 1
        if self.active > 1:
            self.overlap += 1
        self.write_started.set()
        self._release.wait()
        self.active -= 1
        super().write_i2c_block_data(address, register, data)

    def release(self) -> None:
        self._release.set()


def test_a_cancelled_write_does_not_release_the_lock_before_it_lands() -> None:
    """The regression this task's original fix introduced.

    ``asyncio.to_thread`` cannot stop the worker thread once
    ``write_i2c_block_data`` has started — cancelling the coroutine awaiting
    it only stops *waiting*. Without the shield in ``apply_duty``, cancelling
    channel A's ``apply()`` while its write is in flight would unwind
    straight out of ``async with self._lock``, releasing the lock while A's
    write was still running on its own thread — letting channel B start a
    second, genuinely concurrent transaction on the same chip. That is the
    exact interleaving the lock exists to prevent, and it would have passed
    ``test_two_channels_on_one_chip_serialise_their_writes`` above, which
    never cancels anything.
    """
    bus = ReleasableBus()
    device = Pca9685Device(bus)
    a = Pca9685Channel(device, 0, "led-a")
    b = Pca9685Channel(device, 1, "led-b")

    async def scenario() -> None:
        first = asyncio.create_task(a.apply(PwmLevel(duty=0.5)))
        # Wait for A's write to actually be running (blocked) in its thread.
        await asyncio.to_thread(bus.write_started.wait)

        first.cancel()

        # Give the cancellation every chance to (wrongly) unwind through the
        # lock — this sleep is exactly where a premature release would show
        # up as B's write starting.
        second = asyncio.create_task(b.apply(PwmLevel(duty=0.5)))
        try:
            await asyncio.sleep(0.05)
            assert not second.done(), (
                "the second channel's apply() completed while the first channel's "
                "write was still in flight — the lock was released too early"
            )
            assert bus.overlap == 0, "a second write started while the first was still on the wire"
        finally:
            # However the asserts above land, A's real (background) write is
            # blocked on `_release` and must be let go — otherwise this leaves
            # a `ThreadPoolExecutor` worker parked forever, which hangs the
            # whole process at interpreter exit rather than just failing this
            # test.
            bus.release()

        with pytest.raises(asyncio.CancelledError):
            await first
        await second

    run(scenario)
    assert bus.overlap == 0
    # Both writes landed, strictly in order — B could not start until A's
    # (cancelled) write had actually finished.
    assert len(bus.blocks) == 2

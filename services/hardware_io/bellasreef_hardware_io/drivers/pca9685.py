# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""PCA9685 16-channel PWM over I²C — LED dimming (PRD R10a).

**STAGE 1 MEASURED, STAGE 2 PENDING.** On 2026-08-15 the chip at ``0x40`` was
driven from raw ``i2cset``/``i2cget`` — no Bella's Reef code in the loop — with
David's DMM at the **LEDn pin**, no FET stage in the measured chain. Those
readings set two constants in this file, :data:`INVRT_ON` and
:data:`PCA9685_OSC_HZ`. The record is CLAUDE.md, "Stage 1 (PCA9685), CH0 —
PASSED on hardware 2026-08-15".

Two assertions remain outstanding, both David's to make:

1. **Stage 2** — the same duty points through hardware-io and the spine, on the
   same channel and the same probe point, with the meter agreeing to the Stage-1
   numbers. Any divergence is a driver bug by definition.
2. **The fail-safe drills** (process kill, container kill, NATS outage, power
   pull) against a real actuator.

Until both pass, this driver must not be registered on a hub wired to lights.

Output stage, as ruled 2026-08-11
---------------------------------
Recorded as given. Per CLAUDE.md's bench boundary, this file states the design
and the measurements it is waiting on; it does not argue the electronics.

* An **external N-FET stage per channel** (2N7000-class, 1 kΩ gate resistor,
  drain to the dim line and its 10 V pull-up, source to common ground).
* The PCA9685 stays **totem-pole**, driving the FET gate. See
  :data:`OPEN_DRAIN`.
* Polarity is whatever the meter last said, and nothing else — see
  :data:`INVRT_ON`. The 2026-08-11 design *expected* the FET stage to invert;
  that is history, not the governing statement. Stage 1 measured the chip
  directly at the LEDn pin on 2026-08-15 and found no inversion needed, so
  :data:`INVRT_ON` is ``False``. A FET stage inserted later gets **measured**,
  not reasoned about.

Correction trail
----------------
An earlier revision of this driver configured the outputs **open-drain**, for a
bench design in which a 10 V pull-up sat directly on the LEDn pin. That design
was withdrawn on 2026-08-11: LEDn absolute maximum is 5.5 V and the outputs are
5.5 V-only tolerant, so a 10 V pull-up was out of spec. The FET stage above
replaces it, and when that stage is on the bench the DMM goes on the **FET
drain, never the LEDn pin**. The 2026-08-15 Stage 1 had no FET stage in the
chain and was therefore probed at the LEDn pin itself.

Left in the file deliberately. The withdrawn design is the reason this driver's
polarity handling exists at all, and a reader who does not know it was wrong
would eventually reinvent it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, Protocol
from uuid import uuid4

from bellasreef_contracts import ActuatorLevel, ActuatorRegistration, ChipState, PwmLevel
from bellasreef_service import get_logger

from bellasreef_hardware_io.drivers.dimming import light_registration, snap_duty

log = get_logger(__name__)


async def _to_thread_uncancellable[**P](
    func: Callable[P, None], *args: P.args, **kwargs: P.kwargs
) -> None:
    """Run a blocking bus call in a thread; a cancellation cannot leave it running.

    ``asyncio.to_thread`` cannot stop the worker thread once a write has
    started — cancelling the *awaiting* coroutine only stops this coroutine
    from waiting on it, and the ``smbus2`` transaction keeps running to
    completion regardless. Left unshielded, a cancellation racing a write
    inside :meth:`Pca9685Device.apply_duty` or :meth:`Pca9685Device.initialise`
    would unwind straight out of ``async with self._lock`` and release the
    lock while the write was still on the wire — the exact interleaving the
    lock exists to prevent.

    Shielding the offloaded task keeps a cancellation from reaching it; if the
    caller is cancelled anyway, we still wait for the thread to actually
    finish before letting ``CancelledError`` continue past us, so nothing
    downstream — the lock release, or a caller that reacts to the
    cancellation by issuing its own write right after — ever observes a write
    still in flight.
    """
    task: asyncio.Task[None] = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


__all__ = [
    "INVRT_ON",
    "OPEN_DRAIN",
    "PCA9685_OSC_HZ",
    "PCA9685_PRE_SCALE",
    "I2CBus",
    "Pca9685Channel",
    "Pca9685Device",
    "duty_to_counts",
    "registration",
]

# ------------------------------------------------------------------ registers
#
# PCA9685 datasheet register map. MODE1/MODE2/PRE_SCALE semantics corroborated
# against the real chip on 2026-08-09 (CLAUDE.md, "Verified host facts"):
# MODE1 read 0x11 (SLEEP|ALLCALL), MODE2 read 0x04 (OUTDRV), PRE_SCALE read
# 0x1e (30, the power-on default ≈196.9 Hz).

_MODE1: Final = 0x00
_MODE2: Final = 0x01
_LED0_ON_L: Final = 0x06
_ALL_LED_ON_L: Final = 0xFA
_PRE_SCALE: Final = 0xFE

# MODE1 bits
_M1_RESTART: Final = 0x80
_M1_AI: Final = 0x20
_M1_SLEEP: Final = 0x10
_M1_ALLCALL: Final = 0x01

# MODE2 bits
_M2_INVRT: Final = 0x10
_M2_OUTDRV: Final = 0x04

#: The 13th bit of the 4-count LED register block: "full on" / "full off".
#: Using these rather than 0/4095 counts is what makes hard off actually hard —
#: a 0-count off-time still emits a one-tick sliver on some silicon.
_FULL: Final = 0x10

_CHANNELS: Final = 16
_COUNTS: Final = 4096

# --------------------------------------------------------------- our decisions

#: Totem-pole, driving the external N-FET gate. Ruled 2026-08-11.
#:
#: Was open-drain, for a withdrawn bench design that put a 10 V pull-up on the
#: LEDn pin directly — out of spec, since LEDn absolute maximum is 5.5 V. The
#: FET stage carries the 10 V line now and the PCA9685 only ever drives a gate.
#:
#: ``False`` clears MODE2 OUTDRV... which is backwards from the name, so: this
#: flag reads "are we in open-drain", and totem-pole is OUTDRV **set**.
OPEN_DRAIN: Final = False

#: Output logic inversion. **Set by measurement, never by argument.**
#:
#: ``False`` — MODE2 INVRT stays clear. Measured 2026-08-15 (CLAUDE.md, "Stage 1
#: (PCA9685), CH0 — PASSED on hardware 2026-08-15"): raw ``i2cset``/``i2cget``,
#: MODE2 ``0x04``, DMM at the LEDn pin with no FET stage in the chain. David's
#: readings, recorded as given:
#:
#:   0 % -> 0 V · 8.008 % -> 265 mV · 25 % -> 828 mV · 50 % -> 1.654 V ·
#:   75 % -> 2.481 V · 100 % -> 3.307 V, linear within 0.04 percentage points.
#:
#: So duty 0.0 is already dark with INVRT clear. Setting INVRT would put 3.307 V
#: on this channel when it is commanded to its declared safe state — the code
#: had assumed inversion and the bench found none.
#:
#: **The meter decides.** This constant flips on a new measurement of the stage
#: that actually ships, and on nothing else. If the external FET stage of the
#: 2026-08-11 design is inserted later, that chain gets measured at the FET
#: drain and this constant follows the reading; it is not derived, argued or
#: inherited. One constant, one place, one number from a DMM.
#:
#: The stake: the contract declares ``PwmLevel(duty=0.0)`` the safe state,
#: meaning dark. Get this wrong and the declared safe state is the dangerous
#: one, and every fail-safe drill passes in software over a lit tank.
INVRT_ON: Final = False

#: The chip's internal oscillator, **measured, not taken from the datasheet**.
#:
#: The datasheet says 25 MHz. This chip says otherwise, at two prescalers
#: (CLAUDE.md, Stage 1 PCA9685 item 2, measured 2026-08-15):
#:
#:   PRE_SCALE 11 -> 544.7/544.8 Hz, implying 26 773 094 Hz
#:   PRE_SCALE 4  -> 1307 Hz,        implying 26 767 360 Hz
#:
#: A constant ratio, ~7.1 % fast, stable across a 2.4x frequency span — so one
#: calibration number per chip is enough. Dividing a requested frequency by a
#: hardcoded 25 MHz is how you ask for 500 Hz and silently get 545.
PCA9685_OSC_HZ: Final = 26_770_000

#: PRE_SCALE for the pinned 500 Hz, computed from the measured oscillator:
#: ``round(26_770_000 / (4096 x 500)) - 1`` = 12, which lands at ≈502.7 Hz.
#:
#: 500 Hz is pinned from bench findings (CLAUDE.md): the XLG-AB dimming window
#: is 100 Hz–3 kHz, with documented spurious triggering above 2 kHz at 10–15%
#: duty, so the usable window is 100 Hz–2 kHz. 500 Hz sits mid-window, clear of
#: both the flicker floor and the spurious region. The chip's own default of 30
#: (≈196.9 Hz) is inside the window but only ~2x above its floor, and it was
#: never a chosen value — just what the silicon powers up with.
#:
#: This was 11, which is the right prescaler for a chip whose oscillator runs at
#: the datasheet's 25 MHz (≈508.6 Hz) and the wrong one for this chip, where it
#: measured ≈545 Hz.
#:
#: Both numbers above belong to **the one chip on this bench**. The (unapproved)
#: spec ``docs/superpowers/specs/2026-08-15-driver-hardware-config.md`` moves
#: ``osc_hz`` / ``pwm_hz`` / ``invert`` into per-device configuration; until it
#: lands, these are the measured constants for that chip.
PCA9685_PRE_SCALE: Final = round(PCA9685_OSC_HZ / (4096 * 500)) - 1


class I2CBus(Protocol):
    """The narrow slice of SMBus this driver needs.

    A protocol rather than a direct ``smbus2`` import so the whole driver is
    exercisable without hardware — which, given that nothing here has driven a
    real light yet, is the only way it is exercisable at all.
    """

    def read_byte_data(self, address: int, register: int) -> int: ...
    def write_byte_data(self, address: int, register: int, value: int) -> None: ...
    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None: ...


def duty_to_counts(duty: float) -> tuple[int, int]:
    """Map a contract duty to ``(on_count, off_count)`` register values.

    Returns the raw 13-bit register pair, where :data:`_FULL` in the high
    position means full-on or full-off rather than a counted edge.

    Three regions, and the middle one is a product decision:

    * ``0.0`` — hard off. Unambiguous, outside the undefined band, and the
      declared safe state, so it must never land inside it.
    * ``0 < duty < 0.08`` — **snaps to 0**, per the session-4 ruling. The XLG
      output is undefined here and a ramp crosses the band at dawn and dusk
      every day.
    * ``>= 0.08`` — passed through as a counted duty.
    """
    snapped = snap_duty(duty)
    if snapped <= 0.0:
        return (0, _FULL << 8)
    if snapped >= 1.0:
        return (_FULL << 8, 0)
    return (0, min(round(snapped * _COUNTS), _COUNTS - 1))


class Pca9685Device:
    """One PCA9685 chip. Owns the registers; channels borrow it.

    Split from :class:`Pca9685Channel` because the frequency, output mode and
    polarity are properties of the *chip*, and sixteen channels each believing
    they configure them independently is how two of them end up disagreeing.
    """

    def __init__(self, bus: I2CBus, address: int = 0x40, *, bus_no: int = 1) -> None:
        self._bus = bus
        self._address = address
        #: Which I²C bus this chip is on. Not used for any register access —
        #: the caller already opened ``bus`` on it — only to name the chip in
        #: :meth:`chip_state`'s instance string, matching
        #: ``capabilities.discover_pca9685``'s ``bus`` detail.
        self._bus_no = bus_no
        self._initialised = False
        #: Captured by :meth:`initialise` at its own PRE_SCALE readback, and
        #: by :meth:`ensure_initialised` respectively. Both ``None`` until the
        #: chip has actually been initialised; :meth:`chip_state` refuses to
        #: assemble a message from either being unset rather than fabricate a
        #: "0" that looks like a measurement.
        self._pre_scale_read_back: int | None = None
        self._initialised_at: datetime | None = None
        #: Serializes every offloaded bus transaction against this chip —
        #: sixteen channels' worth of ``apply``/``drive_safe`` and this chip's
        #: own ``initialise``, all reached via ``asyncio.to_thread``. Without
        #: it two concurrent writes could land as interleaved bytes on the
        #: register block, since each write is now real wall-clock time on a
        #: worker thread rather than one atomic hop on the loop.
        self._lock = asyncio.Lock()

    async def ensure_initialised(self) -> None:
        """:meth:`initialise` exactly once per chip, however many channels open.

        Sixteen channels share one chip. Each one's ``open()`` lands here, and
        only the first does the work: re-running the sleep/PRE_SCALE/restart
        sequence for channel 7 would black out channels 0-6, which are already
        up and possibly already driven.
        """
        if self._initialised:
            return
        await self.initialise()
        self._initialised = True
        self._initialised_at = datetime.now(UTC)

    async def initialise(self) -> None:
        """Put the chip into the configuration this project has decided on.

        Ordering is dictated by the silicon: **PRE_SCALE is only writable while
        SLEEP is set.** The prescaler is latched from the sleeping oscillator,
        so writing it on a running chip silently does nothing and presents as
        "the frequency setting is ignored". Sleep, write, wake, wait ≥500 µs for
        the oscillator, then RESTART.

        The prescaler is read back and asserted rather than assumed, because
        "silently does nothing" is precisely the failure mode.

        The whole sequence is one ``smbus2`` transaction after another — ten
        blocking bus calls in a row — so it runs off the loop in a worker
        thread, under this chip's lock, same as every other write to it. See
        :attr:`_lock`. The offload is cancellation-shielded (see
        :func:`_to_thread_uncancellable`) so a cancellation here cannot
        release the lock mid-sequence either.
        """
        async with self._lock:
            await _to_thread_uncancellable(self._initialise_sync)

        log.info(
            "pca9685 initialised",
            extra={
                "address": hex(self._address),
                "pre_scale": PCA9685_PRE_SCALE,
                "open_drain": OPEN_DRAIN,
                "invrt": INVRT_ON,
                "bench_verified": False,
            },
        )

    def _initialise_sync(self) -> None:
        """The blocking register sequence :meth:`initialise` offloads.

        Runs in a worker thread — every line here is a synchronous bus call or
        (for the oscillator settle) ``time.sleep``, never ``asyncio.sleep``,
        which only means anything on the event-loop thread.
        """
        # All channels hard off before anything else. This runs on a chip whose
        # previous state is unknown — a crashed process, a warm restart — and
        # configuring frequency underneath live outputs is not a thing to do
        # over a tank.
        self.all_off()

        mode1 = self._bus.read_byte_data(self._address, _MODE1)
        self._bus.write_byte_data(self._address, _MODE1, (mode1 & ~_M1_RESTART) | _M1_SLEEP)
        self._bus.write_byte_data(self._address, _PRE_SCALE, PCA9685_PRE_SCALE)

        read_back = self._bus.read_byte_data(self._address, _PRE_SCALE)
        if read_back != PCA9685_PRE_SCALE:
            raise RuntimeError(
                f"PRE_SCALE readback is {read_back}, expected {PCA9685_PRE_SCALE}. "
                "The prescaler is only writable while SLEEP is set; a write to a "
                "running chip is silently discarded."
            )
        # Captured for chip_state(): the assertion above already proved this
        # equals PCA9685_PRE_SCALE, so this is a record of the measurement
        # having been taken, not a new fact.
        self._pre_scale_read_back = read_back

        mode2 = self._bus.read_byte_data(self._address, _MODE2)
        mode2 = (mode2 & ~_M2_OUTDRV) if OPEN_DRAIN else (mode2 | _M2_OUTDRV)
        mode2 = (mode2 | _M2_INVRT) if INVRT_ON else (mode2 & ~_M2_INVRT)
        self._bus.write_byte_data(self._address, _MODE2, mode2)

        # Wake, with auto-increment on so a channel write is one block.
        awake = (mode1 & ~_M1_SLEEP) | _M1_AI | _M1_ALLCALL
        self._bus.write_byte_data(self._address, _MODE1, awake)
        # ≥500 µs for the oscillator to stabilise before RESTART. Datasheet
        # minimum; 1 ms because a sleep this short is not worth shaving.
        time.sleep(0.001)
        self._bus.write_byte_data(self._address, _MODE1, awake | _M1_RESTART)

    async def apply_duty(self, channel: int, duty: float) -> None:
        """Write one channel's duty, off the event loop.

        Serialized against every other offloaded write to this chip — this
        channel's, its fifteen siblings', and :meth:`initialise` — by
        :attr:`_lock`, so two commands landing at once cannot interleave their
        bytes on the wire. The offload is cancellation-shielded (see
        :func:`_to_thread_uncancellable`): a caller cancelled mid-write still
        cannot make the lock available to a second caller until this write has
        actually finished on the wire, which is the property the lock exists
        to provide in the first place.
        """
        async with self._lock:
            await _to_thread_uncancellable(self.write_channel, channel, duty)

    def write_channel(self, channel: int, duty: float) -> None:
        if not 0 <= channel < _CHANNELS:
            raise ValueError(f"channel {channel} out of range 0-{_CHANNELS - 1}")
        on, off = duty_to_counts(duty)
        self._bus.write_i2c_block_data(
            self._address,
            _LED0_ON_L + 4 * channel,
            [on & 0xFF, (on >> 8) & 0xFF, off & 0xFF, (off >> 8) & 0xFF],
        )

    def all_off(self) -> None:
        """Every channel hard off, in one write.

        One transaction rather than sixteen: this is what runs on shutdown and
        on interlock trip, and sixteen chances to be interrupted halfway is
        fifteen too many.
        """
        self._bus.write_i2c_block_data(self._address, _ALL_LED_ON_L, [0x00, 0x00, 0x00, _FULL])

    def chip_state(self) -> ChipState:
        """What this chip is configured as, right now — chip state on the
        wire (spec 2026-08-19, ruled 2026-08-18: option A, a per-chip
        Hardware surface, not identity in a capability's ``detail``).

        No I/O: every value here was already captured by
        :meth:`ensure_initialised` / :meth:`initialise`, which is why calling
        this before the chip has been initialised is a programming error
        rather than a missing reading — app.py only calls it after
        ``open()`` (which initialises the chip) has succeeded.
        """
        if self._pre_scale_read_back is None or self._initialised_at is None:
            raise RuntimeError(
                "chip_state() called before the chip was initialised — "
                "ensure_initialised() must run first"
            )
        return ChipState(
            message_id=uuid4(),
            emitted_at=datetime.now(UTC),
            source="hardware-io",
            hardware_source="pca9685",
            instance=f"{hex(self._address)}@{self._bus_no}",
            initialised=True,
            initialised_at=self._initialised_at,
            facts={
                "address": hex(self._address),
                "bus": self._bus_no,
                "pre_scale": PCA9685_PRE_SCALE,
                "frequency_hz": round(PCA9685_OSC_HZ / (4096 * (PCA9685_PRE_SCALE + 1)), 1),
                "oscillator_hz": PCA9685_OSC_HZ,
                "invrt": INVRT_ON,
                "open_drain": OPEN_DRAIN,
                "channels": _CHANNELS,
                "pre_scale_read_back": self._pre_scale_read_back,
            },
        )


class Pca9685Channel:
    """One PWM channel, as an :class:`ActuatorDriver`.

    Implements the driver protocol structurally; see
    ``docs/contracts/driver-interface.md``.
    """

    def __init__(
        self,
        device: Pca9685Device,
        channel: int,
        actuator_id: str,
        *,
        driver_id: str = "pca9685",
    ) -> None:
        self._device = device
        self._channel = channel
        self._actuator_id = actuator_id
        self._driver_id = driver_id
        self._last: float = 0.0

    @property
    def driver_id(self) -> str:
        return self._driver_id

    @property
    def actuator_id(self) -> str:
        return self._actuator_id

    @property
    def device(self) -> Pca9685Device:
        """The chip this channel shares with up to fifteen others.

        Public so app.py can reach :meth:`Pca9685Device.chip_state` at the
        bring-up moment — the channel is what ``_build_from_registry`` has in
        hand right after ``open()``, not the chip.
        """
        return self._device

    @property
    def safe_state(self) -> ActuatorLevel:
        """Dark.

        The one line in this file that the bench has to confirm rather than
        take on trust — see the module docstring. Everything downstream, the
        supervisor's safe-state assertion included, believes this.
        """
        return PwmLevel(duty=0.0)

    # ----------------------------------------------------------- lifecycle

    async def open(self) -> None:
        """Bring the channel up: make sure its chip is configured.

        This is the ``ActuatorDriver.open()`` hardware-io calls on every
        actuator before registering it. Without it the chip is driven on
        whatever registers it powered up with or the last process left
        behind — which is how Stage 2 on 2026-08-17 measured every voltage
        correctly and the frequency at Stage 1's leftover 545 Hz:
        ``initialise()`` was tested and never called, because ``app.py``
        duck-typed the hook and this class had none. ``open()`` is a
        required Protocol member since, so that gap is a type error now.
        The chip work is per-chip and idempotent; see
        :meth:`Pca9685Device.ensure_initialised`.
        """
        await self._device.ensure_initialised()

    async def apply(self, level: ActuatorLevel) -> None:
        if not isinstance(level, PwmLevel):
            raise TypeError(f"{self._actuator_id} is a PWM channel; got {type(level).__name__}")
        await self._device.apply_duty(self._channel, level.duty)
        self._last = level.duty

    async def drive_safe(self) -> None:
        """Hard off, without consulting anything.

        No spine, no engine, no database — this is called precisely when those
        are gone. Still an I2C transaction, still offloaded: the heartbeat
        watcher that calls this is the one thing in the process that must never
        itself stall, and it is also the caller a wedged bus would otherwise
        stall hardest.
        """
        await self._device.apply_duty(self._channel, 0.0)
        self._last = 0.0

    async def read_back(self) -> ActuatorLevel | None:
        """``None``: the PCA9685 cannot report its own output.

        The registers read back what was written, which confirms the I²C write
        landed and says nothing about whether the LED driver did anything with
        it. Reporting that as "confirmed" would turn a commanded value into a
        measured one, and the distinction is what makes a stuck output
        detectable. Honest ``None`` beats a confident echo.
        """
        return None

    def effective_level(self, level: ActuatorLevel) -> ActuatorLevel:
        """What :meth:`apply` would actually put on the pin — pure, no I²C.

        hardware-io-internal Protocol member (safety.py's
        ``SafetyActuatorDriver``), not part of the contracts
        ``ActuatorDriver``. Applies the same :func:`~.dimming.snap_duty` rule
        :func:`duty_to_counts` uses, so the two can never disagree: this is
        what lets safety.py's max-runtime clock key on what the hardware
        actually does instead of what was commanded (2026-08-29 finding —
        without this, a snap-band command like 5% read as "not at safe
        state" even though the pin sits hard off, the same silent divergence
        the 2026-08-23 truth-line publish fix (spine.py) caught for the wire).
        """
        if not isinstance(level, PwmLevel):
            raise TypeError(f"{self._actuator_id} is a PWM channel; got {type(level).__name__}")
        return PwmLevel(duty=snap_duty(level.duty))


def registration(
    channel: Pca9685Channel,
    *,
    max_runtime_s: float = 18 * 3600.0,
    heartbeat_timeout_s: float = 30.0,
) -> ActuatorRegistration:
    """This channel's registration, from the shared light rule.

    Shared with the RP1 PWM driver on purpose: a channel's safety contract must
    not change because the wiring moved to different silicon.
    """
    return light_registration(
        actuator_id=channel.actuator_id,
        driver_id=channel.driver_id,
        safe_state=channel.safe_state,
        max_runtime_s=max_runtime_s,
        heartbeat_timeout_s=heartbeat_timeout_s,
    )

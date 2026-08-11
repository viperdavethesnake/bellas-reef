# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""PCA9685 16-channel PWM over I²C — LED dimming (PRD R10a).

**BENCH-UNVERIFIED.** Everything here is exercised against a fake bus and
nothing here has driven a real light. Two assertions are outstanding and both
require David at the bench:

1. **duty 0.0 measures dark**, on a meter or a lamp, not by reasoning about the
   datasheet. See :data:`INVRT_ON` — that is the knob, and its current setting
   is a hypothesis.
2. **The fail-safe drills** (process kill, container kill, NATS outage, power
   pull) against a real actuator.

Until both pass, this driver must not be registered on a hub wired to lights.

Why (1) is the whole ballgame
-----------------------------
The chip powers up in totem-pole (``MODE2 = 0x04``, OUTDRV set — measured on
this hardware, 2026-08-09). R10a calls for **open-drain**, because parallel
Mean Well XLG-AB-class drivers share a dimming line and a totem-pole output
driving it high fights every other output on that line.

Open-drain changes what the load sees. The pin only *sinks*: the idle level
comes from an external pull-up, so the register's "on" period pulls the dimming
input LOW, where totem-pole with ``INVRT=0`` drove it HIGH. Flip OUTDRV without
re-examining INVRT and the output inverts.

That inversion is a safety inversion, not a cosmetic one. Our contract declares
``PwmLevel(duty=0.0)`` to be the safe state, meaning dark. If the output is
inverted at the hardware, **duty 0.0 drives the channel to full brightness** —
the declared safe state becomes the most dangerous one, and every fail-safe
drill passes in software while the tank sits at 100%.

So the polarity decision is written down here as code, in one place, with a
test — and then it is still not trusted until someone has looked at a lamp.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from bellasreef_contracts import ActuatorLevel, PwmLevel
from bellasreef_service import get_logger

if TYPE_CHECKING:
    from bellasreef_contracts import ActuatorRegistration

log = get_logger(__name__)

__all__ = [
    "INVRT_ON",
    "MIN_USABLE_DUTY",
    "OPEN_DRAIN",
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

#: Open-drain, per R10a: parallel XLG-AB drivers share the dimming line and a
#: totem-pole output would fight every other output on it.
OPEN_DRAIN: Final = True

#: Output logic inversion. **This is the bench knob.**
#:
#: Reasoning, which is not the same as evidence: in open-drain the pin only
#: sinks, so with ``INVRT=0`` a register duty of 0.0 means "never sink", the
#: external pull-up holds the dimming input HIGH, and a driver that dims on a
#: HIGH input goes to full output. Setting INVRT makes duty 0.0 sink
#: continuously and hold the line LOW.
#:
#: That chain has one unverified link — whether these particular XLG drivers
#: read LOW as off — and it is exactly the link that decides whether our
#: declared safe state is dark or blinding. Hence bench item 1. If the lamp says
#: otherwise, this constant flips and nothing else changes, which is the entire
#: reason the decision lives in one named place.
INVRT_ON: Final = True

#: PRE_SCALE for ~500 Hz on the internal 25 MHz oscillator:
#: 25e6 / (4096 x (11+1)) ≈ 508.6 Hz.
#:
#: Pinned from bench findings (CLAUDE.md): the XLG-AB dimming window is
#: 100 Hz–3 kHz, with documented spurious triggering above 2 kHz at 10–15% duty,
#: so the usable window is 100 Hz–2 kHz. 500 Hz sits mid-window, clear of both
#: the flicker floor and the spurious region. The chip's own default of 30
#: (≈196.9 Hz) is inside the window but only ~2x above its floor, and it was
#: never a chosen value — just what the silicon powers up with.
PCA9685_PRE_SCALE: Final = 11

#: Below this the XLG output is undefined — it may flicker, sit dark, or go to
#: full. This is a property of the LED driver, not something software can smooth
#: over.
#:
#: Ruled in session-4 planning and recorded in CLAUDE.md "Verified host facts":
#: **anything under 8% snaps to 0.** Not clamped up to 0.08. A diurnal ramp
#: crosses this band twice every day, so dawn and dusk are the normal path
#: through it, not an edge case — and of the two options, snapping down is the
#: one that cannot leave a channel emitting light at a duty the hardware refuses
#: to define.
MIN_USABLE_DUTY: Final = 0.08


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
    if duty <= 0.0 or duty < MIN_USABLE_DUTY:
        return (0, _FULL << 8)
    if duty >= 1.0:
        return (_FULL << 8, 0)
    return (0, min(round(duty * _COUNTS), _COUNTS - 1))


class Pca9685Device:
    """One PCA9685 chip. Owns the registers; channels borrow it.

    Split from :class:`Pca9685Channel` because the frequency, output mode and
    polarity are properties of the *chip*, and sixteen channels each believing
    they configure them independently is how two of them end up disagreeing.
    """

    def __init__(self, bus: I2CBus, address: int = 0x40) -> None:
        self._bus = bus
        self._address = address

    async def initialise(self) -> None:
        """Put the chip into the configuration this project has decided on.

        Ordering is dictated by the silicon: **PRE_SCALE is only writable while
        SLEEP is set.** The prescaler is latched from the sleeping oscillator,
        so writing it on a running chip silently does nothing and presents as
        "the frequency setting is ignored". Sleep, write, wake, wait ≥500 µs for
        the oscillator, then RESTART.

        The prescaler is read back and asserted rather than assumed, because
        "silently does nothing" is precisely the failure mode.
        """
        # All channels hard off before anything else. This runs on a chip whose
        # previous state is unknown — a crashed process, a warm restart — and
        # configuring frequency underneath live outputs is not a thing to do
        # over a tank.
        self._bus.write_i2c_block_data(self._address, _ALL_LED_ON_L, [0x00, 0x00, 0x00, _FULL])

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

        mode2 = self._bus.read_byte_data(self._address, _MODE2)
        mode2 = (mode2 & ~_M2_OUTDRV) if OPEN_DRAIN else (mode2 | _M2_OUTDRV)
        mode2 = (mode2 | _M2_INVRT) if INVRT_ON else (mode2 & ~_M2_INVRT)
        self._bus.write_byte_data(self._address, _MODE2, mode2)

        # Wake, with auto-increment on so a channel write is one block.
        awake = (mode1 & ~_M1_SLEEP) | _M1_AI | _M1_ALLCALL
        self._bus.write_byte_data(self._address, _MODE1, awake)
        # ≥500 µs for the oscillator to stabilise before RESTART. Datasheet
        # minimum; 1 ms because a sleep this short is not worth shaving.
        await asyncio.sleep(0.001)
        self._bus.write_byte_data(self._address, _MODE1, awake | _M1_RESTART)

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
    def safe_state(self) -> ActuatorLevel:
        """Dark.

        The one line in this file that the bench has to confirm rather than
        take on trust — see the module docstring. Everything downstream, the
        supervisor's safe-state assertion included, believes this.
        """
        return PwmLevel(duty=0.0)

    async def apply(self, level: ActuatorLevel) -> None:
        if not isinstance(level, PwmLevel):
            raise TypeError(f"{self._actuator_id} is a PWM channel; got {type(level).__name__}")
        self._device.write_channel(self._channel, level.duty)
        self._last = level.duty

    async def drive_safe(self) -> None:
        """Hard off, without consulting anything.

        No spine, no engine, no database — this is called precisely when those
        are gone.
        """
        self._device.write_channel(self._channel, 0.0)
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


def registration(
    channel: Pca9685Channel,
    *,
    max_runtime_s: float = 18 * 3600.0,
    heartbeat_timeout_s: float = 30.0,
) -> ActuatorRegistration:
    """The registration for one LED channel, with the full R1 safety triple.

    ``authoritative`` because hardware-io handles nothing else
    (device-classes.md §3): the bus is local, synchronous and deterministic, and
    a write either lands or raises. That declaration is what obliges the other
    three fields, and PRD R1 rejects the registration without them.

    ``max_runtime_s`` defaults to 18 hours. This is a *runaway* bound, not a
    photoperiod: a reef light legitimately runs 10-12 hours a day, so anything
    near that would trip on a normal Tuesday and teach the operator to ignore
    it. 18 hours is comfortably longer than any real schedule and far shorter
    than "forever", which is what a stuck-on channel would otherwise be.

    ``heartbeat_timeout_s`` is 30s, matching the drill actuator. Lose the
    control engine for half a minute and the channel goes dark — which for a
    light is a visible, survivable failure, and the reason lighting was chosen
    as the first actuator at all.
    """
    from uuid import uuid4

    from bellasreef_contracts import ActuatorRegistration

    return ActuatorRegistration(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=channel.actuator_id,
        actuator_class="pwm",
        role="light",
        driver_id=channel.driver_id,
        control_authority="authoritative",
        failsafe_capable=True,
        transport="local",
        safe_state=channel.safe_state,
        max_runtime_s=max_runtime_s,
        heartbeat_timeout_s=heartbeat_timeout_s,
    )

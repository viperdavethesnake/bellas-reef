# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""What this hub's hardware can offer.

Tier one of the registry. hardware-io looks at the machine it is running on and
announces what it finds — PWM channels, the 1-Wire bus, a PCA9685 if one
answers — without any opinion about what those things are *for*.

That separation is the whole design. A capability is a fact: this hub has PWM
channels. A device is a decision: channel 0 is the blue LED in the display tank.
Conflating them is what made a config file the source of truth and left the app
with nothing to show until somebody edited YAML over SSH.

Discovery is read-only. Nothing here exports a PWM channel, opens an I²C
transaction beyond a presence check, or touches a pin — announcing what exists
must never change what it is doing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from bellasreef_contracts import CapabilityAnnouncement, CapabilityChannel
from bellasreef_service import get_logger

log = get_logger(__name__)

__all__ = [
    "PWM_CHANNEL_GPIO",
    "PWM_CHIP",
    "RP1_PWM0_DEVICE",
    "RP1_PWM_COMPATIBLE",
    "W1_DEVICES",
    "discover_pwm",
    "discover_w1",
    "find_pwm_chip",
]

PWM_CHIP = Path("/sys/class/pwm/pwmchip0")
W1_DEVICES = Path("/sys/bus/w1/devices")

#: Which GPIO each PWM channel reaches. **Verified on this board 2026-08-12**
#: (CLAUDE.md, Verified host facts): ``dtoverlay=pwm-2chan,pin=12,func=4,
#: pin2=13,func2=4`` muxes channel 0 -> GPIO12 and channel 1 -> GPIO13,
#: confirmed with ``pinctrl get 12,13``.
#:
#: This map is also the announcement filter: a channel absent from it reaches
#: no pin, and a pinless channel is not something the hub can offer — it
#: exports in sysfs and drives nothing, which is the trap the host notes call
#: out. Changing the overlay therefore means updating this map in the same
#: commit, the same discipline the pinned PWM frequency follows.
PWM_CHANNEL_GPIO: dict[int, int] = {0: 12, 1: 13}

#: The RP1's first PWM block — the one the overlay muxes to header pins.
#: The SECOND instance (1f0009c000.pwm) drives the fan header; announcing it
#: would offer the fan as lighting. Both measured on this board 2026-08-13.
RP1_PWM0_DEVICE: Final = "1f00098000.pwm"
RP1_PWM_COMPATIBLE: Final = "raspberrypi,rp1-pwm"


def find_pwm_chip(pwm_class: Path = Path("/sys/class/pwm")) -> Path | None:
    """Locate the RP1 PWM0 chip by hardware identity, never by index.

    The pwmchipN index has moved between kernel releases (CLAUDE.md, verified
    host facts), so each class entry is resolved to the device it fronts and
    matched on the block name plus the device-tree compatible.
    """
    if not pwm_class.is_dir():
        return None
    for entry in sorted(pwm_class.iterdir()):
        try:
            device = (entry / "device").resolve()
        except OSError:
            continue
        if device.name != RP1_PWM0_DEVICE:
            continue
        try:
            compatible = (device / "of_node" / "compatible").read_bytes()
        except OSError:
            log.critical(
                "RP1 PWM0 block found but its compatible is unreadable",
                extra={"chip": str(entry)},
            )
            return None
        if RP1_PWM_COMPATIBLE not in compatible.decode(errors="replace"):
            log.critical(
                "the device at the RP1 PWM0 address is not an rp1-pwm",
                extra={"chip": str(entry), "compatible": compatible.decode(errors="replace")},
            )
            return None
        return entry
    log.critical("no RP1 PWM0 block under %s — pi-pwm will not be announced", pwm_class)
    return None


def discover_pwm(chip: Path = PWM_CHIP) -> CapabilityAnnouncement | None:
    """The Pi's own pin-backed PWM channels.

    ``npwm`` bounds the count (our Pi reports 4 where the archived HAL states
    2 — recorded in CLAUDE.md, and why this asks the hardware instead of
    hardcoding a number), and :data:`PWM_CHANNEL_GPIO` filters it: only
    channels the overlay muxes to a pin are announced. The RP1's other
    channels export in sysfs and drive nothing; announcing them offered the
    operator a device the engine would command, with green telemetry, and no
    output — met in the app on 2026-08-13 as two adoptable ghosts.
    """
    if not chip.is_dir():
        return None
    try:
        npwm = int((chip / "npwm").read_text().strip())
    except (OSError, ValueError):
        log.warning("pwm chip present but npwm unreadable", extra={"chip": str(chip)})
        return None

    channels = []
    for index in range(npwm):
        gpio = PWM_CHANNEL_GPIO.get(index)
        if gpio is None:
            continue
        detail: dict[str, str | int | float | bool] = {"chip": chip.name, "gpio": gpio}
        channels.append(CapabilityChannel(channel=str(index), detail=detail))

    return CapabilityAnnouncement(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        hardware_source="pi-pwm",
        channels=channels,
    )


def discover_w1(root: Path = W1_DEVICES) -> CapabilityAnnouncement | None:
    """Probes the 1-Wire bus has enumerated.

    Discoverable hardware, so these announce themselves and then wait: a probe
    the operator has not adopted is visible through the API and inert. A probe
    that starts publishing under a name nobody chose is how a second tank's
    readings end up in the first tank's history.

    Family code ``28`` is the DS18B20. Other families on the same bus are not
    claimed — announcing something no driver here can read would offer the
    operator a device that cannot work.
    """
    if not root.is_dir():
        return None

    channels = [
        CapabilityChannel(
            channel=path.name,
            detail={"family": "28", "bus_master": "w1_bus_master1"},
        )
        for path in sorted(root.iterdir())
        if path.name.startswith("28-")
    ]

    return CapabilityAnnouncement(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        hardware_source="w1-bus",
        channels=channels,
    )

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

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from bellasreef_contracts import CapabilityAnnouncement, CapabilityChannel
from bellasreef_service import get_logger

log = get_logger(__name__)

__all__ = [
    "PINCTRL",
    "RP1_PWM0_DEVICE",
    "RP1_PWM_COMPATIBLE",
    "W1_DEVICES",
    "discover_pwm",
    "discover_w1",
    "find_pwm_chip",
    "parse_pinctrl",
    "read_pwm_mux",
]

W1_DEVICES = Path("/sys/bus/w1/devices")

#: /usr/bin, verified with ``command -v`` on the host 2026-08-13 — NOT
#: /usr/sbin, where a plan pinned it without checking and discovery failed
#: at the next boot. Absolute so the service's PATH is not a variable here.
PINCTRL: Final = "/usr/bin/pinctrl"

#: One pinctrl line: "12: a0    pd | lo // GPIO12 = PWM0_CHAN0"
_PINCTRL_LINE = re.compile(r"//\s*GPIO(\d+)\s*=\s*PWM0_CHAN(\d+)\s*$")

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
    log.critical(
        "no RP1 PWM0 block found — pi-pwm will not be announced",
        extra={"pwm_class": str(pwm_class)},
    )
    return None


def parse_pinctrl(output: str) -> dict[int, int]:
    """channel -> gpio for every pin the mux ties to the RP1 PWM0 block."""
    mux: dict[int, int] = {}
    for line in output.splitlines():
        if match := _PINCTRL_LINE.search(line):
            mux[int(match.group(2))] = int(match.group(1))
    return mux


def read_pwm_mux() -> dict[int, int] | None:
    """The live pin mux, from ``pinctrl get``. None means it could not be
    read — which callers must treat as "announce nothing", never as "nothing
    is muxed": a hub that cannot see the mux must not guess at it.
    """
    try:
        result = subprocess.run(
            [PINCTRL, "get"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.critical(
            "pinctrl could not run — pi-pwm will not be announced",
            extra={"error": str(exc)},
        )
        return None
    if result.returncode != 0:
        log.critical(
            "pinctrl failed — pi-pwm will not be announced",
            extra={"returncode": result.returncode, "stderr": result.stderr.strip()},
        )
        return None
    return parse_pinctrl(result.stdout)


def discover_pwm(
    pwm_class: Path = Path("/sys/class/pwm"),
    mux_reader: Callable[[], dict[int, int] | None] = read_pwm_mux,
) -> CapabilityAnnouncement | None:
    """The RP1's pin-backed PWM channels, read from the live mux.

    The announcement mirrors the operator's overlay: whatever ``pinctrl``
    says is muxed to the PWM0 block is what the hub offers, bounded by the
    chip's ``npwm``. There is no hand-maintained map to drift — an overlay
    change is reflected at the next startup, and a mux that cannot be read
    announces nothing rather than guessing (the two pinless RP1 channels
    shipped as adoptable ghosts on 2026-08-13; never again by construction).

    A readable mux with nothing muxed announces an EMPTY list — known-empty
    is a fact the registry needs in order to prune, distinct from
    unknown (``None``), which stays silent and leaves the registry's last
    good answer standing.
    """
    chip = find_pwm_chip(pwm_class)
    if chip is None:
        return None
    try:
        npwm = int((chip / "npwm").read_text().strip())
    except (OSError, ValueError):
        log.warning("pwm chip present but npwm unreadable", extra={"chip": str(chip)})
        return None

    mux = mux_reader()
    if mux is None:
        return None
    channels = [
        CapabilityChannel(channel=str(index), detail={"chip": chip.name, "gpio": mux[index]})
        for index in sorted(mux)
        if index < npwm
    ]
    if not channels:
        # Known-empty is announced, not suppressed: an empty list is how the
        # registry learns to prune (the contract: "a source that loses a
        # channel can say so by republishing a shorter list"). Suppressing it
        # left two stale channels in the registry after an overlay change on
        # 2026-08-13. Only an UNREADABLE mux (None, above) stays silent.
        log.warning("the RP1 PWM0 block has no pins muxed — announcing an empty pi-pwm")

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

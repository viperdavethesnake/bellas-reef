# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Capability discovery announces what the hub can offer — not what merely exists.

The RP1 block exports four PWM channels in sysfs, and two of them reach no
pin under the overlay this host runs. A channel that exports happily while
driving nothing is the trap CLAUDE.md records from the bench: adopting one
would make a device the engine commands, with green telemetry, and a dark
tank. Discovery filters to pin-backed channels so the registry's meaning is
"offerable", and the operator never meets the ghost.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest
from bellasreef_contracts import CapabilityAnnouncement
from bellasreef_hardware_io import app
from bellasreef_hardware_io.capabilities import (
    PINCTRL,
    discover_pca9685,
    discover_pwm,
    find_pwm_chip,
)
from bellasreef_hardware_io.capabilities import (
    parse_pinctrl as _parse,
)


def _rp1_class(tmp_path: Path) -> Path:
    devices = tmp_path / "devices"
    chip = devices / "1f00098000.pwm" / "pwm" / "pwmchip0"
    chip.mkdir(parents=True)
    (chip / "npwm").write_text("4\n")
    of_node = devices / "1f00098000.pwm" / "of_node"
    of_node.mkdir()
    (of_node / "compatible").write_bytes(b"raspberrypi,rp1-pwm\x00")
    (chip / "device").symlink_to(devices / "1f00098000.pwm")
    pwm_class = tmp_path / "class-pwm"
    pwm_class.mkdir()
    (pwm_class / "pwmchip0").symlink_to(chip)
    return pwm_class


#: pinctrl output exactly as this board prints it (2026-08-13).
TWO_MUXED = """\
12: a0    pd | lo // GPIO12 = PWM0_CHAN0
13: a0    pd | lo // GPIO13 = PWM0_CHAN1
18: no    pd | -- // GPIO18 = none
19: no    pd | -- // GPIO19 = none
"""

FOUR_MUXED = TWO_MUXED.replace(
    "18: no    pd | -- // GPIO18 = none", "18: a3    pd | lo // GPIO18 = PWM0_CHAN2"
).replace("19: no    pd | -- // GPIO19 = none", "19: a3    pd | lo // GPIO19 = PWM0_CHAN3")


class TestDiscoverPwm:
    """The announcement mirrors the operator's overlay, read from the live
    mux. Two channels muxed -> two announced; the full four-channel setup ->
    four announced, zero code change. Unreadable mux -> honest absence."""

    def test_two_muxed_channels_announce_two(self, tmp_path: Path) -> None:
        announcement = discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: _parse(TWO_MUXED))
        assert announcement is not None
        assert [(c.channel, c.detail["gpio"]) for c in announcement.channels] == [
            ("0", 12),
            ("1", 13),
        ]

    def test_four_muxed_channels_announce_four(self, tmp_path: Path) -> None:
        announcement = discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: _parse(FOUR_MUXED))
        assert announcement is not None
        assert [(c.channel, c.detail["gpio"]) for c in announcement.channels] == [
            ("0", 12),
            ("1", 13),
            ("2", 18),
            ("3", 19),
        ]

    def test_channels_beyond_npwm_are_never_announced(self, tmp_path: Path) -> None:
        """npwm still bounds the mux: a pin claiming a channel the chip does
        not report must not conjure one."""
        pwm_class = _rp1_class(tmp_path)
        chip = (pwm_class / "pwmchip0").resolve()
        (chip / "npwm").write_text("1\n")
        announcement = discover_pwm(pwm_class, mux_reader=lambda: _parse(FOUR_MUXED))
        assert announcement is not None
        assert [c.channel for c in announcement.channels] == ["0"]

    def test_an_unreadable_mux_announces_nothing(self, tmp_path: Path) -> None:
        assert discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: None) is None

    def test_a_readable_mux_with_nothing_muxed_announces_empty(self, tmp_path: Path) -> None:
        """Known-empty is a fact worth publishing: an empty channel list is how
        the registry learns to prune (the contract: 'a source that loses a
        channel can say so by republishing a shorter list'). Only an UNREADABLE
        mux stays silent — found live 2026-08-13, when a failed discovery left
        two stale channels in the registry indefinitely."""
        announcement = discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: {})
        assert announcement is not None
        assert announcement.channels == []

    def test_pinctrl_is_where_dpkg_puts_it(self) -> None:
        """/usr/bin, not /usr/sbin — asserted here because the wrong path
        shipped once, 'pinned' in a plan without being verified on the host."""
        assert PINCTRL == "/usr/bin/pinctrl"

    def test_a_missing_chip_announces_nothing(self, tmp_path: Path) -> None:
        empty = tmp_path / "class-pwm"
        empty.mkdir()
        assert discover_pwm(empty, mux_reader=lambda: _parse(TWO_MUXED)) is None


class TestReadPwmMux:
    def test_this_boards_output_parses(self) -> None:
        assert _parse(TWO_MUXED) == {0: 12, 1: 13}

    def test_garbage_yields_no_reading(self) -> None:
        assert _parse("not pinctrl output at all\n") == {}


class TestFindPwmChip:
    """The chip index has moved between kernel releases (CLAUDE.md, verified
    host facts) and the second RP1 PWM instance drives the fan header.
    Announcing by index risks offering the fan as lighting; identity is the
    only safe address."""

    def _sysfs(self, tmp_path: Path, chips: dict[str, tuple[str, str | None]]) -> Path:
        """Build /sys/class/pwm with symlinks into a fake device tree.

        chips maps class entry name -> (device block name, compatible or None).
        """
        devices = tmp_path / "devices"
        pwm_class = tmp_path / "class-pwm"
        pwm_class.mkdir()
        for entry, (block, compatible) in chips.items():
            chip_dir = devices / block / "pwm" / entry
            chip_dir.mkdir(parents=True)
            (chip_dir / "npwm").write_text("4\n")
            if compatible is not None:
                of_node = devices / block / "of_node"
                of_node.mkdir(exist_ok=True)
                (of_node / "compatible").write_bytes(compatible.encode() + b"\x00")
                (chip_dir / "device").symlink_to(devices / block)
            (pwm_class / entry).symlink_to(chip_dir)
        return pwm_class

    def test_the_rp1_pwm0_block_is_found_wherever_its_index_lands(self, tmp_path: Path) -> None:
        """The fan's block sits at index 0 here — the bounce CLAUDE.md warns
        about — and identity must still pick ours at index 1."""
        pwm_class = self._sysfs(
            tmp_path,
            {
                "pwmchip0": ("1f0009c000.pwm", "raspberrypi,rp1-pwm"),
                "pwmchip1": ("1f00098000.pwm", "raspberrypi,rp1-pwm"),
            },
        )
        chip = find_pwm_chip(pwm_class)
        assert chip is not None
        assert chip.name == "pwmchip1"

    def test_a_matching_name_with_the_wrong_compatible_is_refused(self, tmp_path: Path) -> None:
        pwm_class = self._sysfs(tmp_path, {"pwmchip0": ("1f00098000.pwm", "some,other-pwm")})
        assert find_pwm_chip(pwm_class) is None

    def test_no_rp1_block_present_finds_nothing(self, tmp_path: Path) -> None:
        pwm_class = self._sysfs(tmp_path, {"pwmchip0": ("1f0009c000.pwm", "raspberrypi,rp1-pwm")})
        assert find_pwm_chip(pwm_class) is None

    def test_a_missing_class_directory_finds_nothing(self, tmp_path: Path) -> None:
        assert find_pwm_chip(tmp_path / "absent") is None


class FakeI2CBus:
    """Records every transaction, so "one read, zero writes" is assertable.

    Discovery's whole contract on the I²C bus is a presence check. A fake that
    only returned a value would let a driver-shaped discovery — one that woke
    the chip, or read six registers to be sure — pass silently.
    """

    def __init__(self, mode1: int | None = 0x11, *, close_fails: bool = False) -> None:
        self._mode1 = mode1
        self._close_fails = close_fails
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int, object]] = []
        self.closed = False

    def read_byte_data(self, address: int, register: int) -> int:
        self.reads.append((address, register))
        if self._mode1 is None:
            raise OSError(121, "Remote I/O error")
        return self._mode1

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        self.writes.append((address, register, value))

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        self.writes.append((address, register, data))

    def close(self) -> None:
        self.closed = True
        if self._close_fails:
            raise OSError(5, "Input/output error")


def _dev(tmp_path: Path) -> Path:
    node = tmp_path / "i2c-1"
    node.write_bytes(b"")
    return node


class TestDiscoverPca9685:
    """Announce the chip when it answers; announce empty when the bus is there
    and nothing does; stay silent when the bus itself is absent.

    Same three-way split as discover_pwm, for the same reason: known-empty is
    how the registry prunes a chip that was unplugged, and unknown must leave
    the last good answer standing.
    """

    def test_a_chip_that_answers_announces_sixteen_channels(self, tmp_path: Path) -> None:
        bus = FakeI2CBus()
        announcement = discover_pca9685(lambda _: bus, dev=_dev(tmp_path))
        assert announcement is not None
        assert announcement.hardware_source == "pca9685"
        assert [c.channel for c in announcement.channels] == [str(n) for n in range(16)]
        assert announcement.channels[0].detail == {
            "bus": 1,
            "address": "0x40",
            "mode1": "0x11",
        }

    def test_the_presence_check_is_one_read_and_no_writes(self, tmp_path: Path) -> None:
        """Discovery is read-only — 'an I²C transaction beyond a presence
        check' is what this module forbids itself. A chip left asleep at
        power-on must still be asleep afterwards."""
        bus = FakeI2CBus()
        discover_pca9685(lambda _: bus, dev=_dev(tmp_path))
        assert bus.reads == [(0x40, 0x00)]
        assert bus.writes == []

    def test_nothing_answering_announces_empty_not_silence(self, tmp_path: Path) -> None:
        bus = FakeI2CBus(mode1=None)
        announcement = discover_pca9685(lambda _: bus, dev=_dev(tmp_path))
        assert announcement is not None
        assert announcement.hardware_source == "pca9685"
        assert announcement.channels == []

    def test_a_missing_bus_node_announces_nothing_and_opens_nothing(self, tmp_path: Path) -> None:
        opened: list[int] = []

        def opener(bus: int) -> FakeI2CBus:
            opened.append(bus)
            return FakeI2CBus()

        assert discover_pca9685(opener, dev=tmp_path / "absent") is None
        assert opened == []

    def test_an_opener_that_cannot_import_smbus2_announces_nothing(self, tmp_path: Path) -> None:
        """A dev machine without smbus2 is not a hub whose chip vanished."""

        def opener(bus: int) -> FakeI2CBus:
            raise ImportError("No module named 'smbus2'")

        assert discover_pca9685(opener, dev=_dev(tmp_path)) is None

    def test_an_unopenable_bus_announces_nothing(self, tmp_path: Path) -> None:
        def opener(bus: int) -> FakeI2CBus:
            raise OSError(13, "Permission denied")

        assert discover_pca9685(opener, dev=_dev(tmp_path)) is None

    def test_the_bus_is_closed_afterwards(self, tmp_path: Path) -> None:
        """Discovery runs at startup and must not leak the fd it opened."""
        bus = FakeI2CBus()
        discover_pca9685(lambda _: bus, dev=_dev(tmp_path))
        assert bus.closed is True

    def test_the_bus_is_closed_even_when_nothing_answers(self, tmp_path: Path) -> None:
        bus = FakeI2CBus(mode1=None)
        discover_pca9685(lambda _: bus, dev=_dev(tmp_path))
        assert bus.closed is True

    def test_a_failing_close_does_not_lose_the_answer(self, tmp_path: Path) -> None:
        """The probe has already succeeded by the time the bus is closed.
        Discovery runs at startup, before the liveness guard, so an exception
        on the way out is a crash-loop — not a fault the service survives."""
        bus = FakeI2CBus(close_fails=True)
        announcement = discover_pca9685(lambda _: bus, dev=_dev(tmp_path))
        assert announcement is not None
        assert len(announcement.channels) == 16

    def test_bus_and_address_are_overridable(self, tmp_path: Path) -> None:
        bus = FakeI2CBus()
        announcement = discover_pca9685(lambda _: bus, bus=3, address=0x41, dev=_dev(tmp_path))
        assert announcement is not None
        assert announcement.channels[0].detail == {
            "bus": 3,
            "address": "0x41",
            "mode1": "0x11",
        }
        assert bus.reads == [(0x41, 0x00)]


class _RecordingSpine:
    def __init__(self) -> None:
        self.published: list[CapabilityAnnouncement] = []

    async def publish_capabilities(self, announcement: CapabilityAnnouncement) -> None:
        self.published.append(announcement)


def _announcement(source: Literal["pi-pwm", "pca9685", "w1-bus"]) -> CapabilityAnnouncement:
    return CapabilityAnnouncement(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        hardware_source=source,
        channels=[],
    )


class TestAnnounceCapabilities:
    """Every discovery the module offers has to reach the wire.

    The PCA9685 was implemented end to end — driver, factory, API literal, iOS
    adopt sheet — and stayed invisible for want of three words in this loop.
    A test that counts the publishes is what makes the next source's omission
    fail rather than merely not appear.
    """

    def test_all_three_sources_are_announced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, "discover_pwm", lambda: _announcement("pi-pwm"))
        monkeypatch.setattr(app, "discover_w1", lambda: _announcement("w1-bus"))
        monkeypatch.setattr(app, "discover_pca9685", lambda _opener: _announcement("pca9685"))

        hardware = app.HardwareIO()
        spine = _RecordingSpine()
        hardware.spine = cast(Any, spine)
        asyncio.run(hardware._announce_capabilities())

        assert [a.hardware_source for a in spine.published] == ["pi-pwm", "w1-bus", "pca9685"]

    def test_one_source_raising_does_not_silence_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This loop runs before httpd.start() and liveness.start(). An
        exception escaping a discover_* would kill the process at startup and
        crash-loop it under `restart: unless-stopped` — so a hub whose I²C bus
        misbehaves would stop reporting its temperature probe too."""

        def explode() -> CapabilityAnnouncement | None:
            raise RuntimeError("the bus did something nobody planned for")

        monkeypatch.setattr(app, "discover_pwm", lambda: _announcement("pi-pwm"))
        monkeypatch.setattr(app, "discover_w1", explode)
        monkeypatch.setattr(app, "discover_pca9685", lambda _opener: _announcement("pca9685"))

        hardware = app.HardwareIO()
        spine = _RecordingSpine()
        hardware.spine = cast(Any, spine)
        asyncio.run(hardware._announce_capabilities())

        assert [a.hardware_source for a in spine.published] == ["pi-pwm", "pca9685"]

    def test_a_publish_that_raises_does_not_silence_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same guard, the other half: the spine is inside the try too."""

        class _FailsOnce(_RecordingSpine):
            async def publish_capabilities(self, announcement: CapabilityAnnouncement) -> None:
                if announcement.hardware_source == "pi-pwm":
                    raise RuntimeError("publish failed")
                await super().publish_capabilities(announcement)

        monkeypatch.setattr(app, "discover_pwm", lambda: _announcement("pi-pwm"))
        monkeypatch.setattr(app, "discover_w1", lambda: _announcement("w1-bus"))
        monkeypatch.setattr(app, "discover_pca9685", lambda _opener: _announcement("pca9685"))

        hardware = app.HardwareIO()
        spine = _FailsOnce()
        hardware.spine = cast(Any, spine)
        asyncio.run(hardware._announce_capabilities())

        assert [a.hardware_source for a in spine.published] == ["w1-bus", "pca9685"]

    def test_a_source_that_knows_nothing_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, "discover_pwm", lambda: None)
        monkeypatch.setattr(app, "discover_w1", lambda: _announcement("w1-bus"))
        monkeypatch.setattr(app, "discover_pca9685", lambda _opener: None)

        hardware = app.HardwareIO()
        spine = _RecordingSpine()
        hardware.spine = cast(Any, spine)
        asyncio.run(hardware._announce_capabilities())

        assert [a.hardware_source for a in spine.published] == ["w1-bus"]

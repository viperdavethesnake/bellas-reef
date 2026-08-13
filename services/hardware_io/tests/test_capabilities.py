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

from pathlib import Path

from bellasreef_hardware_io.capabilities import (
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

    def test_a_readable_mux_with_nothing_muxed_announces_nothing(self, tmp_path: Path) -> None:
        assert discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: {}) is None

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

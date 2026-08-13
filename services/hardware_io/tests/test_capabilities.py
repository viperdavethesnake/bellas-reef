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

from bellasreef_hardware_io.capabilities import PWM_CHANNEL_GPIO, discover_pwm


def _chip(tmp_path: Path, npwm: str) -> Path:
    chip = tmp_path / "pwmchip0"
    chip.mkdir()
    (chip / "npwm").write_text(npwm)
    return chip


class TestDiscoverPwm:
    def test_only_pin_backed_channels_are_announced(self, tmp_path: Path) -> None:
        """npwm says 4; the overlay muxes 2. The announcement is the 2."""
        announcement = discover_pwm(_chip(tmp_path, "4\n"))
        assert announcement is not None
        assert [c.channel for c in announcement.channels] == ["0", "1"]

    def test_every_announced_channel_names_its_pin(self, tmp_path: Path) -> None:
        """The gpio in detail is the operator's physical-identity cue at the
        moment of adoption; a pin-backed channel must always carry it."""
        announcement = discover_pwm(_chip(tmp_path, "4\n"))
        assert announcement is not None
        gpios = {c.channel: c.detail["gpio"] for c in announcement.channels}
        assert gpios == {str(i): g for i, g in PWM_CHANNEL_GPIO.items()}

    def test_npwm_smaller_than_the_map_announces_only_real_channels(self, tmp_path: Path) -> None:
        """The kernel's count still bounds the announcement — a mapping entry
        for a channel the chip does not report must not invent one."""
        announcement = discover_pwm(_chip(tmp_path, "1\n"))
        assert announcement is not None
        assert [c.channel for c in announcement.channels] == ["0"]

    def test_a_missing_chip_announces_nothing(self, tmp_path: Path) -> None:
        assert discover_pwm(tmp_path / "absent") is None

    def test_an_unreadable_npwm_announces_nothing(self, tmp_path: Path) -> None:
        chip = tmp_path / "pwmchip0"
        chip.mkdir()
        (chip / "npwm").write_text("not a number")
        assert discover_pwm(chip) is None

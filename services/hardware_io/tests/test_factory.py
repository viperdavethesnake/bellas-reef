# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""build_from_assignments against a drained assignment list that can repeat.

Spine.read_assignments() drains a pull-subscribe with
DeliverPolicy.LAST_PER_SUBJECT, unsubscribed once the drain is done. A deploy
recreates api and hardware-io together, and the api's startup lifespan
republishes every adopted assignment on the same subject a hardware-io boot is
mid-drain of — so the drain can observe one device_id twice: the retained
message plus its own fresher echo on the same subject.

Nothing about ordering makes the earlier copy wrong to have seen — it is
simply stale the instant the newer one lands. So the fix here is not "the
drain must never duplicate" (spine.py's job, untouched by this file) but
"the factory must never act on a device_id twice." Last occurrence wins,
which is safe precisely because a later message on the same subject is never
older than an earlier one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from bellasreef_contracts import DeviceAssignment
from bellasreef_hardware_io import factory as factory_module
from bellasreef_hardware_io.factory import build_from_assignments

#: Any Path stands in for a resolved chip in tests that don't care which one —
#: only the identity-resolution tests below need a *specific* chip. The
#: PiPwmChannel constructor performs no filesystem I/O (RealSysfs.__init__
#: touches nothing either), so a build test needs no fake sysfs at all unless
#: it goes on to call driver.open()/apply() — none of these do.
_A_CHIP = Path("/sys/class/pwm/pwmchip0")


def _pipwm(device_id: str, *, adopted: bool, channel: str = "0") -> DeviceAssignment:
    if adopted:
        return DeviceAssignment(
            message_id=uuid4(),
            emitted_at=datetime.now(UTC),
            source="api",
            device_id=device_id,
            adopted=True,
            role="light",
            driver_type="pi-pwm",
            binding={"channel": channel},
        )
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=False,
    )


def test_the_same_adopted_assignment_twice_builds_exactly_one_device() -> None:
    """The crash scenario: a retained message plus its own republished echo."""
    assignments = [_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-1", adopted=True)]

    actuators, sensors = build_from_assignments(assignments, pwm_chip_root=_A_CHIP)

    assert [a.registration.actuator_id for a in actuators] == ["pi-pwm-1"]
    assert sensors == []


def test_an_adopted_assignment_followed_by_its_tombstone_builds_nothing() -> None:
    """Last wins: the unadopted echo landed after the adopted one, so the
    device is free again by the time the factory acts on it."""
    assignments = [_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-1", adopted=False)]

    actuators, sensors = build_from_assignments(assignments)

    assert actuators == []
    assert sensors == []


def test_an_unadopted_assignment_followed_by_a_re_adopt_builds_one() -> None:
    """Re-adopt echo order: the later adopted copy is the one that counts."""
    assignments = [_pipwm("pi-pwm-1", adopted=False), _pipwm("pi-pwm-1", adopted=True)]

    actuators, sensors = build_from_assignments(assignments, pwm_chip_root=_A_CHIP)

    assert [a.registration.actuator_id for a in actuators] == ["pi-pwm-1"]
    assert sensors == []


def test_dedup_preserves_order_of_last_occurrence_across_distinct_devices() -> None:
    """A duplicate of the first device must not reorder devices seen once."""
    assignments = [
        _pipwm("pi-pwm-1", adopted=True),
        _pipwm("pi-pwm-2", adopted=True),
        _pipwm("pi-pwm-1", adopted=True),
    ]

    actuators, _ = build_from_assignments(assignments, pwm_chip_root=_A_CHIP)

    assert [a.registration.actuator_id for a in actuators] == ["pi-pwm-2", "pi-pwm-1"]


def test_a_collapsed_duplicate_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="bellasreef_hardware_io.factory"):
        build_from_assignments([_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-1", adopted=True)])

    assert any("dedup" in r.getMessage() or "duplicate" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------ pi-pwm chip identity


def test_pipwm_built_on_the_identity_resolved_chip(tmp_path: Path) -> None:
    """Finding 3 / spec dd6a68b: the pwmchipN index moves between kernels; a
    fan-header block renumbered to pwmchip0 would take lighting duty commands
    with every software check green. The factory must use find_pwm_chip's
    answer, not the pwmchip0 default."""
    chip = tmp_path / "pwmchip7"  # deliberately not pwmchip0
    actuators, _ = build_from_assignments([_pipwm("pi-pwm-1", adopted=True)], pwm_chip_root=chip)

    assert actuators[0].driver.chip_root == chip  # type: ignore[attr-defined]


def test_pipwm_skipped_when_no_chip_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hub whose pi-pwm capability never resolved must not fall back to a
    guessed pwmchip0 — the same skip-and-log contract as any other
    unbuildable assignment (TopologyError caught by the per-assignment
    except clause), never a build on an index nobody proved."""
    monkeypatch.setattr(factory_module, "find_pwm_chip", lambda: None)

    actuators, _ = build_from_assignments([_pipwm("pi-pwm-1", adopted=True)])

    assert actuators == []


def test_pipwm_resolves_the_chip_at_most_once_per_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two pi-pwm assignments in one build must trigger exactly one
    filesystem resolution — the second reuses the first's answer rather than
    re-reading /sys/class/pwm, and a failed resolution is not retried against
    every subsequent pi-pwm assignment in the same build either."""
    calls = 0

    def counting_find_pwm_chip() -> Path:
        nonlocal calls
        calls += 1
        return _A_CHIP

    monkeypatch.setattr(factory_module, "find_pwm_chip", counting_find_pwm_chip)

    actuators, _ = build_from_assignments(
        [_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-2", adopted=True)]
    )

    assert calls == 1
    assert {a.registration.actuator_id for a in actuators} == {"pi-pwm-1", "pi-pwm-2"}


def test_pipwm_resolution_failure_is_also_cached_across_a_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure half of the same sentinel: a chip that never resolves must
    not be re-probed for every pi-pwm assignment in the build. Both
    assignments skip (each raises its own TopologyError, caught individually
    by the per-assignment except clause), but find_pwm_chip only ever runs
    once — the second assignment reuses the first's "nothing resolved"
    answer instead of reading /sys/class/pwm again."""
    calls = 0

    def counting_find_pwm_chip() -> Path | None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(factory_module, "find_pwm_chip", counting_find_pwm_chip)

    actuators, _ = build_from_assignments(
        [_pipwm("pi-pwm-1", adopted=True), _pipwm("pi-pwm-2", adopted=True)]
    )

    assert calls == 1
    assert actuators == []


# ------------------------------------------------------------ pca9685 lifecycle


class _Bus:
    """Just enough PCA9685 for the factory path: registers plus block writes."""

    def __init__(self) -> None:
        self.registers: dict[int, int] = {0x00: 0x11, 0x01: 0x04, 0xFE: 0x1E}
        self.blocks: list[tuple[int, list[int]]] = []

    def read_byte_data(self, address: int, register: int) -> int:
        return self.registers.get(register, 0)

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        if register == 0xFE and not self.registers[0x00] & 0x10:
            return  # PRE_SCALE is discarded unless SLEEP is set
        self.registers[register] = value

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        self.blocks.append((register, list(data)))


def _pca(device_id: str, channel: int) -> DeviceAssignment:
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=True,
        role="light",
        driver_type="pca9685",
        binding={"channel": str(channel)},
    )


def test_a_built_pca9685_channel_opens_like_every_other_actuator() -> None:
    """The app brings actuators up via ``driver.open()`` and skips ones without
    it. Stage 2 on 2026-08-17 found the PCA9685 driven on Stage 1's leftover
    registers because its channel had no ``open()`` — the chip was never
    initialised through the stack. A built PCA9685 channel must open, and
    opening must land the measured prescaler on the bus."""
    import asyncio

    from bellasreef_hardware_io.drivers.pca9685 import PCA9685_PRE_SCALE

    buses: list[_Bus] = []

    def open_i2c(bus_no: int) -> _Bus:
        buses.append(_Bus())
        return buses[-1]

    actuators, _ = build_from_assignments([_pca("pca9685-0", 0)], open_i2c=open_i2c)

    assert [a.registration.actuator_id for a in actuators] == ["pca9685-0"]
    opener = getattr(actuators[0].driver, "open", None)
    assert opener is not None, "a PCA9685 channel must expose the open() lifecycle hook"
    asyncio.run(opener())
    assert buses[0].registers[0xFE] == PCA9685_PRE_SCALE

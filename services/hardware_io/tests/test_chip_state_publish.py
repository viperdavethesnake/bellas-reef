# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""ChipState publishers at the three bring-up moments.

Task 3 of the chip-state feature: pca9685 publishes after its chip
initialises, pi-pwm publishes on its first channel's open(), and w1-bus
publishes at capability-announce time (a bus has no open() step of its own).

All three follow the same best-effort shape as ``_publish_state`` — a publish
failure must never fail the actuator's ``open()`` or capability discovery —
and the same publish-once-per-chip-per-process keying, proven here by opening
more than one channel on the same chip and counting publishes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from bellasreef_contracts import (
    ActuatorRegistration,
    CapabilityAnnouncement,
    CapabilityChannel,
    ChipState,
)
from bellasreef_hardware_io import app as app_module
from bellasreef_hardware_io.app import HardwareIO
from bellasreef_hardware_io.drivers.dimming import light_registration
from bellasreef_hardware_io.drivers.pca9685 import (
    INVRT_ON,
    OPEN_DRAIN,
    PCA9685_OSC_HZ,
    PCA9685_PRE_SCALE,
    Pca9685Channel,
    Pca9685Device,
)
from bellasreef_hardware_io.drivers.pipwm import PiPwmChannel
from bellasreef_hardware_io.factory import BuiltActuator

# ------------------------------------------------------------------ fakes


class FakeI2CBus:
    """A PCA9685 powering up as the real one on this bench did — see
    test_pca9685.py's FakeBus, which this mirrors: MODE1 0x11, MODE2 0x04,
    PRE_SCALE 0x1e, and PRE_SCALE writes discarded unless SLEEP is set."""

    _MODE1 = 0x00
    _MODE2 = 0x01
    _PRE_SCALE = 0xFE
    _SLEEP = 0x10

    def __init__(self) -> None:
        self.registers: dict[int, int] = {
            self._MODE1: 0x11,
            self._MODE2: 0x04,
            self._PRE_SCALE: 0x1E,
        }

    def read_byte_data(self, address: int, register: int) -> int:
        return self.registers.get(register, 0)

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        if register == self._PRE_SCALE and not self.registers.get(self._MODE1, 0) & self._SLEEP:
            return
        self.registers[register] = value

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        pass


class FakeSysfs:
    """Just enough of /sys/class/pwm for a channel's open() to succeed.

    Unlike test_pipwm.py's FakeSysfs, this does not enforce the kernel's
    ordering rules — that is proven elsewhere. This one only needs to let
    open() complete so the chip-state publish that follows it can be
    observed.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.exported: set[int] = set()

    def write(self, path: Path, value: str) -> None:
        if path.name == "export":
            self.exported.add(int(value))
            return
        if path.name == "unexport":
            self.exported.discard(int(value))
            return
        self.values[str(path)] = value

    def read(self, path: Path) -> str:
        return self.values[str(path)]

    def exists(self, path: Path) -> bool:
        name = path.name
        if name.startswith("pwm") and name != "pwmchip0":
            return int(name.removeprefix("pwm")) in self.exported
        return True

    def writable(self, path: Path) -> bool:
        return True


def _pwm_chip_root(tmp_path: Path, device_name: str = "1f00098000.pwm") -> Path:
    """A pwmchip directory whose ``device`` symlink resolves to ``device_name``,
    the same shape capabilities.py's ``_rp1_class`` builds for discovery."""
    devices = tmp_path / "devices"
    device_dir = devices / device_name
    device_dir.mkdir(parents=True)
    chip_root = tmp_path / "pwmchip0"
    chip_root.mkdir()
    (chip_root / "device").symlink_to(device_dir)
    return chip_root


class _RecordingSpine:
    """Records assignments (empty, for _build_from_registry), capability
    announcements and chip states — everything one process's bring-up touches.
    """

    def __init__(self, *, chip_state_raises: bool = False) -> None:
        self.chip_states: list[ChipState] = []
        self.capabilities: list[CapabilityAnnouncement] = []
        self._chip_state_raises = chip_state_raises

    async def read_assignments(self) -> list[object]:
        return []

    async def publish_capabilities(self, announcement: CapabilityAnnouncement) -> None:
        self.capabilities.append(announcement)

    async def publish_chip_state(self, state: ChipState) -> None:
        if self._chip_state_raises:
            raise RuntimeError("spine unreachable")
        self.chip_states.append(state)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def _pca_registration(channel: Pca9685Channel) -> ActuatorRegistration:
    return light_registration(actuator_id=channel.actuator_id, driver_id="pca9685")


def _pwm_registration(channel: PiPwmChannel) -> ActuatorRegistration:
    return light_registration(actuator_id=channel.actuator_id, driver_id="rp1-pwm")


def _service_building(
    monkeypatch: pytest.MonkeyPatch, spine: _RecordingSpine, *built: BuiltActuator
) -> HardwareIO:
    monkeypatch.setattr(
        app_module, "build_from_assignments", lambda assignments, *, open_i2c: (list(built), [])
    )
    service = HardwareIO(metrics_port=0)
    service.spine = cast(Any, spine)
    return service


# ---------------------------------------------------------- (a) pca9685 chip_state


class TestPca9685ChipState:
    def test_after_ensure_initialised_chip_state_has_the_fact_table(self) -> None:
        async def scenario() -> ChipState:
            device = Pca9685Device(FakeI2CBus(), 0x40, bus_no=1)
            await device.ensure_initialised()
            return device.chip_state()

        state = run(scenario)

        assert state.hardware_source == "pca9685"
        assert state.instance == "0x40@1"
        assert state.initialised is True
        assert state.initialised_at is not None
        assert state.initialised_at.tzinfo is not None, "initialised_at must be aware"

        expected_frequency = round(PCA9685_OSC_HZ / (4096 * (PCA9685_PRE_SCALE + 1)), 1)
        assert state.facts == {
            "address": "0x40",
            "bus": 1,
            "pre_scale": PCA9685_PRE_SCALE,
            "frequency_hz": expected_frequency,
            "oscillator_hz": PCA9685_OSC_HZ,
            "invrt": INVRT_ON,
            "open_drain": OPEN_DRAIN,
            "channels": 16,
            "pre_scale_read_back": PCA9685_PRE_SCALE,
        }
        assert "bench_verified" not in state.facts

    def test_the_measured_frequency_is_502_point_7(self) -> None:
        """Pinned per the spec's fact table, so a future constant change that
        silently drifts the frequency fails a test rather than a bench read."""
        assert round(PCA9685_OSC_HZ / (4096 * (PCA9685_PRE_SCALE + 1)), 1) == 502.7

    def test_calling_chip_state_before_initialise_is_a_programming_error(self) -> None:
        device = Pca9685Device(FakeI2CBus())
        with pytest.raises(RuntimeError, match="before the chip was initialised"):
            device.chip_state()

    def test_initialise_register_writes_are_unchanged(self) -> None:
        """The spec constraint: capturing chip_state's inputs must not add,
        remove or reorder a single register write."""

        async def scenario() -> FakeI2CBus:
            bus = FakeI2CBus()
            device = Pca9685Device(bus)
            await device.initialise()
            return bus

        bus = run(scenario)
        # Same assertions test_pca9685.py already makes of a freshly
        # initialised chip: PRE_SCALE landed, MODE2 is totem-pole/uninverted.
        assert bus.registers[FakeI2CBus._PRE_SCALE] == PCA9685_PRE_SCALE
        assert bus.registers[FakeI2CBus._MODE2] == 0x04


# ---------------------------------------------------------- (b) pca9685 app wiring


class TestPca9685AppWiring:
    def test_two_channels_on_one_chip_publish_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device = Pca9685Device(FakeI2CBus(), 0x40, bus_no=1)
        ch0 = Pca9685Channel(device, 0, "light-0")
        ch1 = Pca9685Channel(device, 1, "light-1")
        spine = _RecordingSpine()
        service = _service_building(
            monkeypatch,
            spine,
            BuiltActuator(ch0, _pca_registration(ch0)),
            BuiltActuator(ch1, _pca_registration(ch1)),
        )

        asyncio.run(service._build_from_registry())

        assert len(spine.chip_states) == 1
        assert spine.chip_states[0].hardware_source == "pca9685"
        assert spine.chip_states[0].instance == "0x40@1"
        assert {r.actuator_id for r in service._registrations} == {"light-0", "light-1"}

    def test_two_different_chips_publish_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        device_a = Pca9685Device(FakeI2CBus(), 0x40, bus_no=1)
        device_b = Pca9685Device(FakeI2CBus(), 0x41, bus_no=1)
        ch_a = Pca9685Channel(device_a, 0, "light-a")
        ch_b = Pca9685Channel(device_b, 0, "light-b")
        spine = _RecordingSpine()
        service = _service_building(
            monkeypatch,
            spine,
            BuiltActuator(ch_a, _pca_registration(ch_a)),
            BuiltActuator(ch_b, _pca_registration(ch_b)),
        )

        asyncio.run(service._build_from_registry())

        assert {s.instance for s in spine.chip_states} == {"0x40@1", "0x41@1"}


# ----------------------------------------------------------- (c) pi-pwm app wiring


class TestPiPwmAppWiring:
    def test_first_channel_open_publishes_the_fact_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        chip_root = _pwm_chip_root(tmp_path)
        channel = PiPwmChannel(0, "light-0", chip_root=chip_root, sysfs=FakeSysfs())
        spine = _RecordingSpine()
        service = _service_building(
            monkeypatch, spine, BuiltActuator(channel, _pwm_registration(channel))
        )

        asyncio.run(service._build_from_registry())

        assert len(spine.chip_states) == 1
        state = spine.chip_states[0]
        assert state.hardware_source == "pi-pwm"
        assert state.instance == "1f00098000.pwm"
        assert state.initialised is True
        assert state.initialised_at is not None
        assert state.facts == {
            "chip": "pwmchip0",
            "device": "1f00098000.pwm",
            "period_ns": 2_000_000,
            "frequency_hz": 500.0,
            "polarity": "normal",
            "channels": 4,
        }

    def test_two_channels_on_the_same_chip_publish_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        chip_root = _pwm_chip_root(tmp_path)
        ch0 = PiPwmChannel(0, "light-0", chip_root=chip_root, sysfs=FakeSysfs())
        ch1 = PiPwmChannel(1, "light-1", chip_root=chip_root, sysfs=FakeSysfs())
        spine = _RecordingSpine()
        service = _service_building(
            monkeypatch,
            spine,
            BuiltActuator(ch0, _pwm_registration(ch0)),
            BuiltActuator(ch1, _pwm_registration(ch1)),
        )

        asyncio.run(service._build_from_registry())

        assert len(spine.chip_states) == 1


# --------------------------------------------------------------- (d) w1 at announce


class TestW1ChipStateAtAnnounce:
    def test_announce_publishes_chip_state_with_the_probe_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        announcement = CapabilityAnnouncement(
            message_id=uuid4(),
            emitted_at=datetime.now(UTC),
            source="hardware-io",
            hardware_source="w1-bus",
            channels=[
                CapabilityChannel(
                    channel="28-000000bfe244",
                    detail={"family": "28", "bus_master": "w1_bus_master1"},
                ),
                CapabilityChannel(
                    channel="28-000000000002",
                    detail={"family": "28", "bus_master": "w1_bus_master1"},
                ),
            ],
        )
        monkeypatch.setattr(app_module, "discover_pwm", lambda: None)
        monkeypatch.setattr(app_module, "discover_pca9685", lambda opener: None)
        monkeypatch.setattr(app_module, "discover_w1", lambda: announcement)

        service = HardwareIO(metrics_port=0)
        spine = _RecordingSpine()
        service.spine = cast(Any, spine)

        asyncio.run(service._announce_capabilities())

        assert len(spine.chip_states) == 1
        state = spine.chip_states[0]
        assert state.hardware_source == "w1-bus"
        assert state.instance == "w1_bus_master1"
        assert state.initialised is True
        assert state.initialised_at is not None
        assert state.facts == {"bus_master": "w1_bus_master1", "probes": 2}

    def test_an_empty_bus_still_publishes_with_zero_probes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        announcement = CapabilityAnnouncement(
            message_id=uuid4(),
            emitted_at=datetime.now(UTC),
            source="hardware-io",
            hardware_source="w1-bus",
            channels=[],
        )
        monkeypatch.setattr(app_module, "discover_pwm", lambda: None)
        monkeypatch.setattr(app_module, "discover_pca9685", lambda opener: None)
        monkeypatch.setattr(app_module, "discover_w1", lambda: announcement)

        service = HardwareIO(metrics_port=0)
        spine = _RecordingSpine()
        service.spine = cast(Any, spine)

        asyncio.run(service._announce_capabilities())

        assert spine.chip_states[0].facts["probes"] == 0


# --------------------------------------------------------- (e) best-effort publish


class TestBestEffortPublish:
    def test_a_raising_publish_does_not_fail_open_pca9685(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        device = Pca9685Device(FakeI2CBus(), 0x40, bus_no=1)
        channel = Pca9685Channel(device, 0, "light-0")
        spine = _RecordingSpine(chip_state_raises=True)
        service = _service_building(
            monkeypatch, spine, BuiltActuator(channel, _pca_registration(channel))
        )

        with caplog.at_level(logging.WARNING):
            asyncio.run(service._build_from_registry())

        assert [r.actuator_id for r in service._registrations] == ["light-0"]
        assert spine.chip_states == []
        warnings = [r for r in caplog.records if "failed to publish chip state" in r.message]
        assert len(warnings) == 1
        assert warnings[0].hardware_source == "pca9685"  # type: ignore[attr-defined]
        assert warnings[0].instance == "0x40@1"  # type: ignore[attr-defined]

    def test_a_raising_publish_does_not_fail_open_pipwm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        chip_root = _pwm_chip_root(tmp_path)
        channel = PiPwmChannel(0, "light-0", chip_root=chip_root, sysfs=FakeSysfs())
        spine = _RecordingSpine(chip_state_raises=True)
        service = _service_building(
            monkeypatch, spine, BuiltActuator(channel, _pwm_registration(channel))
        )

        with caplog.at_level(logging.WARNING):
            asyncio.run(service._build_from_registry())

        assert [r.actuator_id for r in service._registrations] == ["light-0"]
        assert spine.chip_states == []
        warnings = [r for r in caplog.records if "failed to publish chip state" in r.message]
        assert len(warnings) == 1

    def test_a_failed_publish_is_retried_by_the_next_channel_on_the_chip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Marked "published" only on success — a chip whose first publish
        failed is not silenced for the rest of the process."""
        device = Pca9685Device(FakeI2CBus(), 0x40, bus_no=1)
        ch0 = Pca9685Channel(device, 0, "light-0")
        ch1 = Pca9685Channel(device, 1, "light-1")
        spine = _RecordingSpine(chip_state_raises=True)
        service = _service_building(
            monkeypatch,
            spine,
            BuiltActuator(ch0, _pca_registration(ch0)),
            BuiltActuator(ch1, _pca_registration(ch1)),
        )
        asyncio.run(service._build_from_registry())
        assert spine.chip_states == []

        spine._chip_state_raises = False
        asyncio.run(service._publish_actuator_chip_state(ch1))
        assert len(spine.chip_states) == 1

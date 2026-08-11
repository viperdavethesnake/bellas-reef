# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The device file, and what it refuses.

hardware-io is topology-driven: what this hub is wired to is a file on the box,
not a branch in ``app.py``. Two properties carry the weight, and both are here.

**A bad entry stops the service.** The tempting alternative — log it and carry
on with the devices that did parse — produces a hub that comes up healthy with
a light missing. That is the same failure as the audit writer that was
constructible and never ran, and as hardware-io publishing nothing without
``BELLASREEF_NATS_URL``: a process that looks fine and is not doing its job.

**Identity survives rebinding.** A device's id is what the subject, the
database row, the phone and the history all key on. Moving a light from the
Pi's own PWM to a PCA9685 board is a wiring change, and none of those should
notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bellasreef_hardware_io.factory import build_actuators, build_sensors
from bellasreef_hardware_io.topology import (
    PiPwmBinding,
    TopologyError,
    load_topology,
)

# David's build, as the worked example: two Pi-native LED channels and two
# probes. The second probe is declared before it is wired, deliberately — the
# config path for multiple 1-Wire devices is proven by this file rather than by
# waiting for hardware.
DAVIDS_BUILD = """
version: 1
actuators:
  - id: led-blue
    role: light
    binding:
      driver: pi-pwm
      channel: 0
  - id: led-white
    role: light
    binding:
      driver: pi-pwm
      channel: 1
sensors:
  - id: display-tank
    binding:
      driver: ds18b20
      rom: 28-000000bfe244
  - id: sump
    binding:
      driver: ds18b20
      rom: 28-0000010c4d91
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "devices.yaml"
    path.write_text(text)
    return path


# ------------------------------------------------------------- the real build


def test_davids_build_parses(tmp_path: Path) -> None:
    topology = load_topology(_write(tmp_path, DAVIDS_BUILD))

    assert [a.id for a in topology.actuators] == ["led-blue", "led-white"]
    assert [s.id for s in topology.sensors] == ["display-tank", "sump"]
    assert all(isinstance(a.binding, PiPwmBinding) for a in topology.actuators)


def test_two_probes_need_no_code_change(tmp_path: Path) -> None:
    """Multi-probe 1-Wire, proven through config rather than through wiring.

    The old path enumerated whatever the kernel had found, so a second probe
    was a hardware event. Declaring it means the second probe exists as far as
    the hub is concerned the moment the file says so — and a declared probe
    that never reports is something the silence watcher can raise an alert
    about, where an undiscovered one is simply invisible.
    """
    topology = load_topology(_write(tmp_path, DAVIDS_BUILD))
    sensors = build_sensors(topology)

    assert len(sensors) == 2
    assert {s.driver_id for s in sensors} == {"display-tank", "sump"}


def test_mixed_driver_types_build_together(tmp_path: Path) -> None:
    """The point of the whole exercise: one hub, two PWM sources."""
    mixed = """
version: 1
actuators:
  - id: led-blue
    binding: {driver: pi-pwm, channel: 0}
  - id: led-red
    binding: {driver: pca9685, channel: 4}
"""
    topology = load_topology(_write(tmp_path, mixed))
    built = build_actuators(topology, open_i2c=lambda bus: _FakeBus())

    assert [b.registration.driver_id for b in built] == ["rp1-pwm", "pca9685"]
    # Same safety contract from both, which is the invariant that makes the
    # choice of silicon genuinely invisible upward.
    assert {b.registration.control_authority for b in built} == {"authoritative"}
    assert {b.registration.role for b in built} == {"light"}
    assert {b.registration.max_runtime_s for b in built} == {18 * 3600.0}


def test_channels_on_one_chip_share_a_device(tmp_path: Path) -> None:
    """Frequency and output mode are chip properties, not channel properties."""
    two_channels = """
version: 1
actuators:
  - id: led-a
    binding: {driver: pca9685, channel: 0}
  - id: led-b
    binding: {driver: pca9685, channel: 1}
"""
    topology = load_topology(_write(tmp_path, two_channels))
    opened: list[int] = []

    def open_i2c(bus: int) -> _FakeBus:
        opened.append(bus)
        return _FakeBus()

    build_actuators(topology, open_i2c=open_i2c)

    assert len(opened) == 1, "each channel opened its own bus; they must share the chip"


# --------------------------------------------------- identity vs binding


def test_rebinding_preserves_identity(tmp_path: Path) -> None:
    """The load-bearing property.

    The same light, moved from the Pi's PWM to a PCA9685 board, keeps its id —
    so its NATS subject, its devices row, its name on the phone and its history
    in VictoriaMetrics all carry on uninterrupted. Only `driver_id` changes,
    which is the honest record of what is now driving it.
    """
    on_pi = """
version: 1
actuators:
  - id: led-blue
    binding: {driver: pi-pwm, channel: 0}
"""
    on_board = """
version: 1
actuators:
  - id: led-blue
    binding: {driver: pca9685, channel: 7}
"""
    before = load_topology(_write(tmp_path, on_pi))
    after = load_topology(_write(tmp_path, on_board))

    first = build_actuators(before)[0]
    second = build_actuators(after, open_i2c=lambda bus: _FakeBus())[0]

    assert first.registration.actuator_id == second.registration.actuator_id == "led-blue"
    assert first.registration.driver_id != second.registration.driver_id
    assert first.registration.safe_state == second.registration.safe_state


# ------------------------------------------------------------- refusals


def test_an_unknown_driver_type_names_the_device(tmp_path: Path) -> None:
    bad = """
version: 1
actuators:
  - id: led-blue
    binding: {driver: some-future-board, channel: 0}
"""
    with pytest.raises(TopologyError) as caught:
        load_topology(_write(tmp_path, bad))

    message = str(caught.value)
    assert "led-blue" in message, "the operator has this file open; name the entry"
    assert "pi-pwm" in message and "pca9685" in message, "say what this build does know"


def test_an_invalid_binding_names_the_device(tmp_path: Path) -> None:
    """npwm is 4 on RP1; channel 9 is not a typo the hub should paper over."""
    bad = """
version: 1
actuators:
  - id: led-white
    binding: {driver: pi-pwm, channel: 9}
"""
    with pytest.raises(TopologyError) as caught:
        load_topology(_write(tmp_path, bad))
    assert "led-white" in str(caught.value)


def test_a_misspelled_binding_field_is_refused_not_defaulted(tmp_path: Path) -> None:
    """`chanel: 1` silently driving channel 0 is the bug a device file invites."""
    bad = """
version: 1
actuators:
  - id: led-blue
    binding: {driver: pi-pwm, chanel: 1}
"""
    with pytest.raises(TopologyError) as caught:
        load_topology(_write(tmp_path, bad))
    assert "led-blue" in str(caught.value)


def test_a_malformed_rom_is_refused(tmp_path: Path) -> None:
    bad = """
version: 1
sensors:
  - id: display-tank
    binding: {driver: ds18b20, rom: "not-a-rom"}
"""
    with pytest.raises(TopologyError) as caught:
        load_topology(_write(tmp_path, bad))
    assert "display-tank" in str(caught.value)


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    """Two devices sharing an id interleave rather than coexist."""
    bad = """
version: 1
actuators:
  - id: led-blue
    binding: {driver: pi-pwm, channel: 0}
  - id: led-blue
    binding: {driver: pi-pwm, channel: 1}
"""
    with pytest.raises(TopologyError, match="duplicate device id"):
        load_topology(_write(tmp_path, bad))


def test_a_missing_file_is_refused_rather_than_assumed_empty(tmp_path: Path) -> None:
    """An empty topology and an absent file are different situations.

    Starting with no devices because the file is missing is a hub that comes up
    green and monitors nothing.
    """
    with pytest.raises(TopologyError, match="no device file"):
        load_topology(tmp_path / "absent.yaml")


def test_broken_yaml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TopologyError, match="not valid YAML"):
        load_topology(_write(tmp_path, "version: 1\nactuators: [unclosed\n"))


def test_an_unknown_file_version_is_refused(tmp_path: Path) -> None:
    """A hub reading a shape it does not know refuses rather than guessing."""
    with pytest.raises(TopologyError):
        load_topology(_write(tmp_path, "version: 99\nactuators: []\n"))


def test_a_reserved_role_is_refused(tmp_path: Path) -> None:
    """`heater` is in the contract and not implemented here.

    Accepting it would register a device nothing knows how to drive — and for a
    heater specifically, that is the actuator the PRD says waits for relay
    drivers and passed drills.
    """
    bad = """
version: 1
actuators:
  - id: heater-main
    role: heater
    binding: {driver: pi-pwm, channel: 2}
"""
    with pytest.raises(TopologyError) as caught:
        load_topology(_write(tmp_path, bad))
    assert "heater-main" in str(caught.value)


class _FakeBus:
    def read_byte_data(self, address: int, register: int) -> int:
        return 0

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        return None

    def write_i2c_block_data(self, address: int, register: int, data: list[int]) -> None:
        return None

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Sensor readings and the driver interface's structural guarantees."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import Heartbeat, SensorReading
from bellasreef_contracts.driver import GpioLine, I2CAddress, OneWireDevice
from pydantic import ValidationError


def _reading(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": uuid4(),
        "emitted_at": datetime.now(UTC),
        "source": "hardware-io",
        "sensor_id": "display-tank",
        "sensor_type": "temp",
        "value": 25.4,
        "unit": "degC",
        "quality": "ok",
    }
    payload.update(overrides)
    return payload


def test_reading_accepts_a_good_sample() -> None:
    assert SensorReading.model_validate(_reading()).value == 25.4


def test_ok_quality_requires_a_value() -> None:
    """A probe that read nothing must say 'fault', not publish a null as healthy."""
    with pytest.raises(ValidationError, match="requires a value"):
        SensorReading.model_validate(_reading(value=None))


def test_fault_may_carry_no_value() -> None:
    reading = SensorReading.model_validate(_reading(value=None, quality="fault"))
    assert reading.value is None


def test_naive_timestamps_are_rejected() -> None:
    """Naive datetimes on a scheduler are a latent incident."""
    with pytest.raises(ValidationError):
        SensorReading.model_validate(_reading(emitted_at=datetime(2026, 8, 9)))  # noqa: DTZ001


def test_heartbeat_requires_positive_interval() -> None:
    base = {
        "message_id": uuid4(),
        "emitted_at": datetime.now(UTC),
        "source": "control-engine",
        "component": "control-engine",
        "sequence": 0,
    }
    with pytest.raises(ValidationError):
        Heartbeat.model_validate({**base, "interval_s": 0.0})


class TestAddressing:
    """The host facts encoded as types rather than documentation."""

    def test_gpio_line_is_addressed_by_label(self) -> None:
        line = GpioLine(chip_label="pinctrl-rp1", offset=4)
        assert line.chip_label == "pinctrl-rp1"

    def test_gpio_line_has_no_chip_index_field(self) -> None:
        """/dev/gpiochipN numbering has moved between kernels — it must be
        impossible to address a line by index."""
        with pytest.raises(ValidationError):
            GpioLine.model_validate({"chip": 0, "offset": 4})

    def test_i2c_address_is_bounded_to_the_7bit_range(self) -> None:
        assert I2CAddress(bus=1, address=0x48).address == 0x48
        with pytest.raises(ValidationError):
            I2CAddress(bus=1, address=0x80)

    def test_onewire_id_shape(self) -> None:
        assert OneWireDevice(device_id="28-0000075d1b2c").device_id.startswith("28-")
        with pytest.raises(ValidationError):
            OneWireDevice(device_id="not-a-1wire-id")

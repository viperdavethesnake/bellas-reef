# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Turning declared topology into live drivers.

The one place that knows how to build each driver type. ``app.py`` knows that
it has actuators and sensors; it does not know that a PCA9685 needs an I²C bus
opened or that an RP1 channel needs exporting, and adding a third PWM source
should not teach it.

Construction is deliberately separate from :mod:`.topology`: parsing a file and
opening hardware fail in different ways and at different times, and a
validation error should not depend on whether a bus happens to be readable.
"""

from __future__ import annotations

from bellasreef_contracts import ActuatorRegistration, DeviceAssignment
from bellasreef_contracts.driver import ActuatorDriver
from bellasreef_service import get_logger

from bellasreef_hardware_io.drivers.dimming import light_registration
from bellasreef_hardware_io.drivers.onewire import DS18B20
from bellasreef_hardware_io.drivers.pca9685 import I2CBus, Pca9685Channel, Pca9685Device
from bellasreef_hardware_io.drivers.pipwm import PiPwmChannel, SysfsWriter
from bellasreef_hardware_io.topology import (
    ActuatorEntry,
    Ds18b20Binding,
    Pca9685Binding,
    PiPwmBinding,
    SensorEntry,
    Topology,
    TopologyError,
)

log = get_logger(__name__)

__all__ = ["BuiltActuator", "OpenI2CBus", "build_actuators", "build_sensors"]

#: Injectable so tests build the real graph without hardware. Production passes
#: nothing and gets the real thing.
OpenI2CBus = None


class BuiltActuator:
    """A driver plus the registration that declares its safety contract."""

    def __init__(self, driver: ActuatorDriver, registration: ActuatorRegistration) -> None:
        self.driver = driver
        self.registration = registration


def build_actuators(
    topology: Topology,
    *,
    open_i2c: object | None = None,
    sysfs: SysfsWriter | None = None,
) -> list[BuiltActuator]:
    """Instantiate every declared actuator, mixing driver types freely.

    One PCA9685 device object is shared by every channel bound to the same
    (bus, address): frequency and output mode are properties of the chip, and
    sixteen channels each configuring them independently is how two of them end
    up disagreeing.
    """
    built: list[BuiltActuator] = []
    chips: dict[tuple[int, int], Pca9685Device] = {}

    for entry in topology.actuators:
        binding = entry.binding
        driver: ActuatorDriver

        if isinstance(binding, PiPwmBinding):
            driver = PiPwmChannel(
                binding.channel,
                entry.id,
                period_ns=binding.period_ns,
                inverted=binding.inverted,
                sysfs=sysfs,
            )
            driver_id = "rp1-pwm"

        elif isinstance(binding, Pca9685Binding):
            key = (binding.bus, binding.address)
            if key not in chips:
                if open_i2c is None:
                    raise TopologyError(
                        f"device {entry.id!r} is bound to a pca9685 on i2c bus "
                        f"{binding.bus}, but no I²C bus was provided to the factory"
                    )
                bus: I2CBus = open_i2c(binding.bus)  # type: ignore[operator]
                chips[key] = Pca9685Device(bus, binding.address)
            driver = Pca9685Channel(chips[key], binding.channel, entry.id)
            driver_id = "pca9685"

        else:  # pragma: no cover — the discriminated union makes this unreachable
            raise TopologyError(f"device {entry.id!r} has an unbuildable binding: {binding!r}")

        built.append(
            BuiltActuator(
                driver,
                light_registration(actuator_id=entry.id, driver_id=driver_id),
            )
        )
        log.info(
            "actuator built",
            extra={"actuator_id": entry.id, "driver_id": driver_id, "role": entry.role},
        )

    return built


def build_from_assignments(
    assignments: list[DeviceAssignment],
    *,
    open_i2c: object | None = None,
    sysfs: SysfsWriter | None = None,
) -> tuple[list[BuiltActuator], list[DS18B20]]:
    """Instantiate exactly what the registry says this hub owns.

    The registry is the source of topology. The device file was, and its epitaph
    is the identity fork it caused: it let a config author mint a new id for
    hardware that already had one, and a tank's history ran down two device_ids
    for seventy minutes.

    Unadopted assignments build nothing. That is the announce-then-adopt state
    made real — a probe the hub can see but nobody has claimed is visible
    through the API and inert, rather than publishing under a name nobody chose.

    A single bad assignment is skipped and logged, not fatal. Unlike the device
    file — where a bad entry stopped the service because the file was the whole
    topology — here it is one device among several, and taking a tank offline
    over one malformed row would be the larger failure.
    """
    actuators: list[BuiltActuator] = []
    sensors: list[DS18B20] = []
    chips: dict[tuple[int, int], Pca9685Device] = {}

    for assignment in assignments:
        if not assignment.adopted:
            log.info(
                "assignment not adopted; nothing built",
                extra={"device_id": assignment.device_id},
            )
            continue
        binding = assignment.binding or {}
        try:
            if assignment.driver_type == "ds18b20":
                from bellasreef_contracts.driver import OneWireDevice

                sensors.append(
                    DS18B20(
                        OneWireDevice(device_id=binding["rom"]),
                        driver_id=assignment.device_id,
                    )
                )
            elif assignment.driver_type == "pi-pwm":
                actuators.append(
                    BuiltActuator(
                        PiPwmChannel(int(binding["channel"]), assignment.device_id, sysfs=sysfs),
                        light_registration(actuator_id=assignment.device_id, driver_id="rp1-pwm"),
                    )
                )
            elif assignment.driver_type == "pca9685":
                bus_no = int(binding.get("bus", 1))
                address = int(binding.get("address", 0x40))
                key = (bus_no, address)
                if key not in chips:
                    if open_i2c is None:
                        raise TopologyError("no I²C bus provided to the factory")
                    chips[key] = Pca9685Device(open_i2c(bus_no), address)  # type: ignore[operator]
                actuators.append(
                    BuiltActuator(
                        Pca9685Channel(chips[key], int(binding["channel"]), assignment.device_id),
                        light_registration(actuator_id=assignment.device_id, driver_id="pca9685"),
                    )
                )
            else:
                raise TopologyError(f"unknown driver type {assignment.driver_type!r}")
        except (KeyError, ValueError, TopologyError) as exc:
            log.error(
                "assignment could not be built; device skipped",
                extra={"device_id": assignment.device_id, "error": str(exc)},
            )
            continue

        log.info(
            "device built from registry",
            extra={
                "device_id": assignment.device_id,
                "driver_type": assignment.driver_type,
            },
        )

    return actuators, sensors


def build_sensors(topology: Topology) -> list[DS18B20]:
    """Instantiate every declared sensor.

    Declared, not discovered. Enumerating whatever the kernel happens to see
    means a probe that falls off the bus simply stops existing, and a hub with
    one probe instead of two looks exactly like a hub that only ever had one.
    A declared probe that is missing is a probe the silence watcher can raise
    an alert about.
    """
    sensors: list[DS18B20] = []
    for entry in topology.sensors:
        binding = entry.binding
        if isinstance(binding, Ds18b20Binding):
            from bellasreef_contracts.driver import OneWireDevice

            sensors.append(
                DS18B20(
                    OneWireDevice(device_id=binding.rom),
                    driver_id=entry.id,
                    poll_interval_s=binding.poll_interval_s,
                    offset_c=binding.offset_c,
                )
            )
            log.info(
                "sensor built",
                extra={"sensor_id": entry.id, "rom": binding.rom},
            )
        else:  # pragma: no cover — unreachable through the discriminated union
            raise TopologyError(f"device {entry.id!r} has an unbuildable binding: {binding!r}")
    return sensors


def describe(entry: ActuatorEntry | SensorEntry) -> str:
    """One line for logs and errors: what it is and where it is bound."""
    return f"{entry.id} -> {entry.binding.driver}"

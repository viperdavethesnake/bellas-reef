# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Turning declared topology into live drivers.

The one place that knows how to build each driver type. ``app.py`` knows that
it has actuators and sensors; it does not know that a PCA9685 needs an I²C bus
opened or that an RP1 channel needs exporting, and adding a third PWM source
should not teach it.
"""

from __future__ import annotations

from pathlib import Path

from bellasreef_contracts import ActuatorRegistration, DeviceAssignment
from bellasreef_contracts.driver import ActuatorDriver
from bellasreef_service import get_logger

from bellasreef_hardware_io.capabilities import find_pwm_chip
from bellasreef_hardware_io.drivers.dimming import light_registration
from bellasreef_hardware_io.drivers.onewire import DS18B20
from bellasreef_hardware_io.drivers.pca9685 import Pca9685Channel, Pca9685Device
from bellasreef_hardware_io.drivers.pipwm import PiPwmChannel, SysfsWriter

log = get_logger(__name__)

__all__ = ["BuiltActuator", "OpenI2CBus", "TopologyError", "build_from_assignments"]

#: Injectable so tests build the real graph without hardware. Production passes
#: nothing and gets the real thing.
OpenI2CBus = None


class TopologyError(RuntimeError):
    """An assignment cannot be built. Always names the device at fault."""


class BuiltActuator:
    """A driver plus the registration that declares its safety contract."""

    def __init__(self, driver: ActuatorDriver, registration: ActuatorRegistration) -> None:
        self.driver = driver
        self.registration = registration


def _last_per_device(assignments: list[DeviceAssignment]) -> list[DeviceAssignment]:
    """Collapse to one assignment per device_id, keeping the last occurrence.

    A deploy recreates api and hardware-io together: the api's startup
    lifespan republishes every adopted assignment, and that republish can land
    while :meth:`Spine.read_assignments` is mid-drain, so the same device_id
    can appear twice in one drained list — the retained message plus its own
    fresher echo on the same subject. The later copy is never older than the
    earlier one, so last-wins is correct here even though nothing in this
    list carries a timestamp to compare: arrival order on a single subject
    already is the ordering.

    Order of the surviving entries follows last occurrence, matching what a
    consumer that only ever saw the final state of each subject would have
    built.
    """
    last_index: dict[str, int] = {}
    for index, assignment in enumerate(assignments):
        last_index[assignment.device_id] = index
    keep = set(last_index.values())
    return [assignment for index, assignment in enumerate(assignments) if index in keep]


def build_from_assignments(
    assignments: list[DeviceAssignment],
    *,
    open_i2c: object | None = None,
    sysfs: SysfsWriter | None = None,
    pwm_chip_root: Path | None = None,
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
    deduped = _last_per_device(assignments)
    if len(deduped) != len(assignments):
        log.info(
            "duplicate assignments collapsed to last-per-device_id",
            extra={"received": len(assignments), "deduped": len(deduped)},
        )

    actuators: list[BuiltActuator] = []
    sensors: list[DS18B20] = []
    chips: dict[tuple[int, int], Pca9685Device] = {}
    #: Distinguishes "not yet attempted" from "attempted and found nothing" —
    #: an injected chip counts as already resolved. Without this, a failed
    #: filesystem resolution (find_pwm_chip() -> None) would be retried on
    #: every subsequent pi-pwm assignment in the same build instead of once,
    #: because a None result cannot itself signal "already tried".
    pwm_chip_resolved = pwm_chip_root is not None

    for assignment in deduped:
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
                if not pwm_chip_resolved:
                    pwm_chip_root = find_pwm_chip()
                    pwm_chip_resolved = True
                if pwm_chip_root is None:
                    # The pwmchipN index has moved between kernel releases
                    # (CLAUDE.md, verified host facts; spec dd6a68b). Building
                    # on a guessed pwmchip0 would take lighting duty commands
                    # on whatever the kernel happened to number that way — a
                    # fan-header block renumbered to pwmchip0 is exactly the
                    # failure this refuses.
                    raise TopologyError(
                        "no RP1 PWM0 chip resolved by identity; refusing to "
                        "build on a guessed pwmchip index (spec dd6a68b)"
                    )
                actuators.append(
                    BuiltActuator(
                        PiPwmChannel(
                            int(binding["channel"]),
                            assignment.device_id,
                            sysfs=sysfs,
                            chip_root=pwm_chip_root,
                        ),
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
                    chips[key] = Pca9685Device(
                        open_i2c(bus_no),  # type: ignore[operator]
                        address,
                        bus_no=bus_no,
                    )
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

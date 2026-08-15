# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""An assignment event after startup must stop the service so the restart
policy rebuilds it from the retained registry. One topology path, by ruling.

The watch is payload-aware because the API republishes every adopted
assignment on every lifespan start (2026-08-15): a message that says exactly
what this process already built is an echo, not a change, and restarting on it
costs a monitoring gap for nothing. Anything else — a different assignment, a
device this process has never heard of, a payload that will not parse — takes
the restart path.
"""

from datetime import UTC, datetime
from uuid import uuid4

from bellasreef_contracts import ActuatorRole, DeviceAssignment
from bellasreef_hardware_io.app import HardwareIO


def _assignment(
    *,
    device_id: str = "dev-1",
    adopted: bool = True,
    role: ActuatorRole | None = "light",
    driver_type: str | None = "pi-pwm",
    binding: dict[str, str] | None = None,
) -> DeviceAssignment:
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=adopted,
        role=role,
        driver_type=driver_type,
        binding={"channel": "0"} if binding is None and adopted else binding,
    )


def test_assignment_event_requests_stop() -> None:
    service = HardwareIO(metrics_port=0)
    assert not service._stopping.is_set()
    service._on_assignment_changed()
    assert service._stopping.is_set()


def test_second_event_is_harmless() -> None:
    service = HardwareIO(metrics_port=0)
    service._on_assignment_changed()
    service._on_assignment_changed()  # burst of adoptions: one restart, no error
    assert service._stopping.is_set()


def test_echo_of_what_was_built_is_ignored() -> None:
    service = HardwareIO(metrics_port=0)
    built = _assignment()
    service._remember_assignments([built])

    # The API's lifespan republish: same device, same everything, new envelope.
    echo = _assignment(device_id=built.device_id)
    service._on_assignment_message(echo.model_dump_json().encode())

    assert not service._stopping.is_set()


def test_unadopted_tombstone_echo_is_ignored() -> None:
    service = HardwareIO(metrics_port=0)
    tomb = _assignment(adopted=False, binding={"channel": "0"})
    service._remember_assignments([tomb])

    service._on_assignment_message(
        _assignment(adopted=False, binding={"channel": "0"}).model_dump_json().encode()
    )

    assert not service._stopping.is_set()


def test_changed_adopted_flag_requests_stop() -> None:
    service = HardwareIO(metrics_port=0)
    service._remember_assignments([_assignment()])

    unbound = _assignment(adopted=False, binding={"channel": "0"})
    service._on_assignment_message(unbound.model_dump_json().encode())

    assert service._stopping.is_set()


def test_changed_binding_requests_stop() -> None:
    service = HardwareIO(metrics_port=0)
    service._remember_assignments([_assignment()])

    service._on_assignment_message(_assignment(binding={"channel": "1"}).model_dump_json().encode())

    assert service._stopping.is_set()


def test_binding_key_order_is_not_a_change() -> None:
    service = HardwareIO(metrics_port=0)
    service._remember_assignments([_assignment(binding={"channel": "0", "bus": "1"})])

    service._on_assignment_message(
        _assignment(binding={"bus": "1", "channel": "0"}).model_dump_json().encode()
    )

    assert not service._stopping.is_set()


def test_unknown_device_requests_stop() -> None:
    service = HardwareIO(metrics_port=0)
    service._remember_assignments([_assignment(device_id="dev-1")])

    service._on_assignment_message(_assignment(device_id="dev-2").model_dump_json().encode())

    assert service._stopping.is_set()


def test_unparseable_payload_requests_stop() -> None:
    service = HardwareIO(metrics_port=0)
    service._remember_assignments([_assignment()])

    service._on_assignment_message(b"{not json")

    assert service._stopping.is_set()

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The safety framework, expressed as tests.

Principle 3 in CLAUDE.md says an actuator that does not declare how it fails
must not be registerable. These tests are what make that true rather than
aspirational.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ActuatorRegistration, BinaryLevel, PwmLevel
from pydantic import ValidationError


def _valid() -> dict[str, Any]:
    return {
        "message_id": uuid4(),
        "emitted_at": datetime.now(UTC),
        "source": "hardware-io",
        "actuator_id": "return-pump",
        "actuator_class": "binary",
        "role": "outlet",
        "driver_id": "gpio-relay-0",
        # The authority axis is required with no default (device-classes.md §2).
        # A GPIO relay is the canonical authoritative device.
        "control_authority": "authoritative",
        "failsafe_capable": True,
        "transport": "local",
        "safe_state": {"kind": "binary", "on": False},
        "max_runtime_s": 3600.0,
        "heartbeat_timeout_s": 15.0,
    }


def test_valid_registration_is_accepted() -> None:
    reg = ActuatorRegistration.model_validate(_valid())
    assert reg.max_runtime_s == 3600.0
    assert isinstance(reg.safe_state, BinaryLevel)
    assert reg.safe_state.on is False


@pytest.mark.parametrize("missing", ["role", "control_authority", "failsafe_capable", "transport"])
def test_registration_rejected_without_a_required_field(missing: str) -> None:
    """Fields with no default at all: absence is a `missing` error on the field."""
    payload = _valid()
    del payload[missing]

    with pytest.raises(ValidationError) as exc:
        ActuatorRegistration.model_validate(payload)

    errors = exc.value.errors()
    assert any(e["loc"] == (missing,) and e["type"] == "missing" for e in errors), errors


@pytest.mark.parametrize("missing", ["safe_state", "max_runtime_s", "heartbeat_timeout_s"])
def test_authoritative_registration_rejected_without_the_safety_triple(missing: str) -> None:
    """The whole point, narrowed: no safe state, no runtime cap, no heartbeat ->
    no *authoritative* device.

    The guarantee is unchanged for every device we actually drive. What moved is
    its scope: docs/device-classes.md §2.2 makes the triple inapplicable to an
    advisory device, which cannot honour it and must therefore be unable to
    claim it. The fields are consequently optional on the model and mandatory in
    the validator, so the rejection is a value error rather than a missing-field
    error — the device is refused either way, which is what R1 is protecting.

    NOTE: this narrows PRD R1 as written ("Every actuator registration declares
    ...") and the CLAUDE.md rule that mirrors it. Flagged for a ruling; not
    resolved here.
    """
    payload = _valid()
    del payload[missing]

    with pytest.raises(ValidationError, match="full safety triple"):
        ActuatorRegistration.model_validate(payload)


@pytest.mark.parametrize("missing", ["safe_state", "max_runtime_s", "heartbeat_timeout_s"])
def test_registration_rejects_explicit_none_for_safety_field(missing: str) -> None:
    """None is not a way to smuggle an unspecified failure mode past the model."""
    payload = _valid()
    payload[missing] = None

    with pytest.raises(ValidationError):
        ActuatorRegistration.model_validate(payload)


@pytest.mark.parametrize("field", ["max_runtime_s", "heartbeat_timeout_s"])
@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_registration_rejects_non_positive_timers(field: str, bad: float) -> None:
    """A zero or negative cap is an unbounded cap wearing a disguise."""
    payload = _valid()
    payload[field] = bad

    with pytest.raises(ValidationError):
        ActuatorRegistration.model_validate(payload)


def test_registration_rejects_safe_state_of_wrong_class() -> None:
    """A PWM safe state on a binary relay is meaningless and must not register."""
    payload = _valid()
    payload["safe_state"] = {"kind": "pwm", "duty": 0.0}

    with pytest.raises(ValidationError, match="does not match"):
        ActuatorRegistration.model_validate(payload)


def test_registration_rejects_unknown_fields() -> None:
    """Unknown keys are a contract mismatch, not something to ignore."""
    payload = _valid()
    payload["max_runtime_seconds"] = 60  # plausible typo for max_runtime_s

    with pytest.raises(ValidationError):
        ActuatorRegistration.model_validate(payload)


def test_pwm_actuator_registers_with_pwm_safe_state() -> None:
    payload = _valid()
    payload["actuator_id"] = "led-channel-a"
    payload["actuator_class"] = "pwm"
    payload["role"] = "light"
    payload["safe_state"] = {"kind": "pwm", "duty": 0.0}

    reg = ActuatorRegistration.model_validate(payload)
    assert isinstance(reg.safe_state, PwmLevel)
    assert reg.safe_state.duty == 0.0


def test_registration_is_frozen() -> None:
    reg = ActuatorRegistration.model_validate(_valid())
    with pytest.raises(ValidationError):
        reg.max_runtime_s = 1.0


@pytest.mark.parametrize("role", ["light", "heater", "pump", "doser", "outlet"])
def test_reserved_roles_are_accepted(role: str) -> None:
    """Reserved values exist so adding them later is not another break."""
    payload = _valid()
    payload["role"] = role
    assert ActuatorRegistration.model_validate(payload).role == role


def test_an_unknown_role_is_refused() -> None:
    """role is a closed set: a client that cannot render it must not receive it."""
    payload = _valid()
    payload["role"] = "disco-ball"
    with pytest.raises(ValidationError):
        ActuatorRegistration.model_validate(payload)


def test_schema_version_is_two() -> None:
    """The envelope version is coarse by design and has not moved.

    v2 was the ``role`` addition. The control-authority axis is a *package*
    MAJOR (contracts 3.0.0) because it adds required fields to an existing
    message, but the shared wire envelope is unchanged — bumping it would
    signal "changed" for every message type that was not touched.
    """
    assert ActuatorRegistration.model_validate(_valid()).schema_version == 2

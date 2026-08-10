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
        "driver_id": "gpio-relay-0",
        "safe_state": {"kind": "binary", "on": False},
        "max_runtime_s": 3600.0,
        "heartbeat_timeout_s": 15.0,
    }


def test_valid_registration_is_accepted() -> None:
    reg = ActuatorRegistration.model_validate(_valid())
    assert reg.max_runtime_s == 3600.0
    assert isinstance(reg.safe_state, BinaryLevel)
    assert reg.safe_state.on is False


@pytest.mark.parametrize("missing", ["safe_state", "max_runtime_s", "heartbeat_timeout_s"])
def test_registration_rejected_without_required_safety_field(missing: str) -> None:
    """The whole point: no safe state, no runtime cap, no heartbeat -> no device."""
    payload = _valid()
    del payload[missing]

    with pytest.raises(ValidationError) as exc:
        ActuatorRegistration.model_validate(payload)

    errors = exc.value.errors()
    assert any(e["loc"] == (missing,) and e["type"] == "missing" for e in errors), errors


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
    payload["safe_state"] = {"kind": "pwm", "duty": 0.0}

    reg = ActuatorRegistration.model_validate(payload)
    assert isinstance(reg.safe_state, PwmLevel)
    assert reg.safe_state.duty == 0.0


def test_registration_is_frozen() -> None:
    reg = ActuatorRegistration.model_validate(_valid())
    with pytest.raises(ValidationError):
        reg.max_runtime_s = 1.0

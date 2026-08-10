# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Command expiry and idempotency.

R4 in the PRD: every actuator command is durable, idempotent, and carries an
expiry; expired commands are dropped and audited, never executed late.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ActuatorCommand
from pydantic import ValidationError

_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


def _valid() -> dict[str, Any]:
    return {
        "message_id": uuid4(),
        "emitted_at": _NOW,
        "source": "control-engine",
        "actuator_id": "ato-pump",
        "actuator_class": "binary",
        "level": {"kind": "binary", "on": True},
        "idempotency_key": uuid4(),
        "expires_at": _NOW + timedelta(seconds=30),
    }


def test_valid_command_is_accepted() -> None:
    cmd = ActuatorCommand.model_validate(_valid())
    assert cmd.expires_at > cmd.emitted_at


@pytest.mark.parametrize("missing", ["expires_at", "idempotency_key"])
def test_command_rejected_without_expiry_or_idempotency_key(missing: str) -> None:
    payload = _valid()
    del payload[missing]

    with pytest.raises(ValidationError) as exc:
        ActuatorCommand.model_validate(payload)

    assert any(e["loc"] == (missing,) for e in exc.value.errors())


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_command_rejects_expiry_not_after_emission(delta: timedelta) -> None:
    """An already-expired command is a programming error, not a valid message."""
    payload = _valid()
    payload["expires_at"] = _NOW + delta

    with pytest.raises(ValidationError, match="strictly after"):
        ActuatorCommand.model_validate(payload)


def test_command_rejects_level_of_wrong_class() -> None:
    payload = _valid()
    payload["level"] = {"kind": "pwm", "duty": 0.5}

    with pytest.raises(ValidationError, match="does not match"):
        ActuatorCommand.model_validate(payload)


def test_pwm_duty_is_bounded() -> None:
    payload = _valid()
    payload["actuator_class"] = "pwm"
    payload["level"] = {"kind": "pwm", "duty": 1.5}

    with pytest.raises(ValidationError):
        ActuatorCommand.model_validate(payload)


class TestIsExpired:
    def test_false_before_expiry(self) -> None:
        cmd = ActuatorCommand.model_validate(_valid())
        assert cmd.is_expired(_NOW + timedelta(seconds=29)) is False

    def test_true_at_and_after_expiry(self) -> None:
        cmd = ActuatorCommand.model_validate(_valid())
        assert cmd.is_expired(_NOW + timedelta(seconds=30)) is True
        assert cmd.is_expired(_NOW + timedelta(hours=1)) is True

    def test_naive_clock_is_refused(self) -> None:
        """Comparing an expiry against a naive clock is the bug this prevents."""
        cmd = ActuatorCommand.model_validate(_valid())
        with pytest.raises(ValueError, match="timezone-aware"):
            cmd.is_expired(datetime(2026, 8, 9, 12, 0, 0))  # noqa: DTZ001

    def test_expiry_is_correct_across_timezones(self) -> None:
        """A consumer in another zone must reach the same verdict.

        The Pi runs America/Los_Angeles and observes DST; the wire is UTC. This
        asserts the comparison is on the instant, not the wall clock.
        """
        cmd = ActuatorCommand.model_validate(_valid())
        pacific = timezone(timedelta(hours=-7))
        later_elsewhere = (_NOW + timedelta(seconds=31)).astimezone(pacific)
        assert cmd.is_expired(later_elsewhere) is True

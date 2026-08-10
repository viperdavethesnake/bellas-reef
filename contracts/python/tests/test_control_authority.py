# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The authority axis on actuator registration — docs/device-classes.md §2.

The asymmetry between §2.1 and §2.2 is the whole point, so it is what these
tests are built around: an authoritative device is rejected for *lacking* the
safety triple, and an advisory device is rejected for *carrying* one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ActuatorRegistration, BinaryLevel, PwmLevel
from pydantic import ValidationError

OFF = BinaryLevel(on=False)

BASE: dict[str, Any] = {
    "message_id": uuid4(),
    "emitted_at": datetime.now(UTC),
    "source": "hardware-io",
    "actuator_id": "relay-1",
    "actuator_class": "binary",
    "role": "outlet",
    "driver_id": "gpio",
}

TRIPLE: dict[str, Any] = {
    "safe_state": OFF,
    "max_runtime_s": 3600.0,
    "heartbeat_timeout_s": 15.0,
}


def registration(**overrides: Any) -> ActuatorRegistration:
    return ActuatorRegistration(**{**BASE, **overrides})


class TestAuthoritative:
    def test_a_complete_authoritative_device_registers(self) -> None:
        made = registration(
            control_authority="authoritative",
            failsafe_capable=True,
            transport="local",
            **TRIPLE,
        )
        assert made.control_authority == "authoritative"

    @pytest.mark.parametrize("missing", ["safe_state", "max_runtime_s", "heartbeat_timeout_s"])
    def test_authoritative_without_the_full_triple_is_rejected(self, missing: str) -> None:
        """R1, scoped to the authority that can honour it."""
        triple = {k: v for k, v in TRIPLE.items() if k != missing}
        with pytest.raises(ValidationError, match="full safety triple"):
            registration(
                control_authority="authoritative",
                failsafe_capable=True,
                transport="local",
                **triple,
            )

    def test_authoritative_must_be_failsafe_capable(self) -> None:
        with pytest.raises(ValidationError, match="failsafe_capable"):
            registration(
                control_authority="authoritative",
                failsafe_capable=False,
                transport="local",
                **TRIPLE,
            )

    def test_authoritative_must_be_local(self) -> None:
        """A network hop is not a control path we can promise anything about."""
        with pytest.raises(ValidationError, match="transport='local'"):
            registration(
                control_authority="authoritative",
                failsafe_capable=True,
                transport="network",
                **TRIPLE,
            )


class TestAdvisory:
    def test_advisory_registers_without_a_safety_triple(self) -> None:
        made = registration(
            control_authority="advisory", failsafe_capable=False, transport="network"
        )
        assert made.safe_state is None

    def test_advisory_with_a_safe_state_is_rejected_not_ignored(self) -> None:
        """§2.2. Accepting and ignoring it would leave a value in the schema that
        reads exactly like an enforced one to everything downstream."""
        with pytest.raises(ValidationError, match="must not declare a safe_state"):
            registration(
                control_authority="advisory",
                failsafe_capable=False,
                transport="network",
                safe_state=OFF,
            )

    def test_advisory_may_be_local(self) -> None:
        """§2.2 says advisory is *typically* network. Typically is not a rule,
        and inventing one would exclude a device we can talk to but not command."""
        assert (
            registration(
                control_authority="advisory", failsafe_capable=False, transport="local"
            ).transport
            == "local"
        )


class TestObserveOnly:
    def test_observe_only_registers_bare(self) -> None:
        made = registration(
            control_authority="observe_only", failsafe_capable=False, transport="network"
        )
        assert made.safe_state is None

    def test_observe_only_with_a_safe_state_is_rejected(self) -> None:
        """§2.3. Stronger than the advisory case, not weaker: no command is ever
        emitted, so nothing could ever apply the value."""
        with pytest.raises(ValidationError, match="must not declare a safe_state"):
            registration(
                control_authority="observe_only",
                failsafe_capable=False,
                transport="network",
                safe_state=OFF,
            )


class TestRequiredness:
    @pytest.mark.parametrize("field", ["control_authority", "failsafe_capable", "transport"])
    def test_all_three_are_required_on_the_wire(self, field: str) -> None:
        """No defaults, by §2.

        A default would mean an unstated authority silently reads as some
        authority — and the only safe-looking default is the strongest one,
        which is precisely the fiction this axis exists to prevent.
        """
        kwargs: dict[str, Any] = {
            "control_authority": "authoritative",
            "failsafe_capable": True,
            "transport": "local",
            **TRIPLE,
        }
        del kwargs[field]
        with pytest.raises(ValidationError, match=field):
            registration(**kwargs)

    def test_an_unknown_authority_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            registration(
                control_authority="best_effort",
                failsafe_capable=False,
                transport="network",
            )


def test_safe_state_still_has_to_match_the_class() -> None:
    """The pre-existing rule survives the safe_state becoming optional."""
    with pytest.raises(ValidationError, match="does not match"):
        registration(
            actuator_class="pwm",
            control_authority="authoritative",
            failsafe_capable=True,
            transport="local",
            safe_state=OFF,
            max_runtime_s=3600.0,
            heartbeat_timeout_s=15.0,
        )
    assert (
        registration(
            actuator_class="pwm",
            control_authority="authoritative",
            failsafe_capable=True,
            transport="local",
            safe_state=PwmLevel(duty=0.0),
            max_runtime_s=3600.0,
            heartbeat_timeout_s=15.0,
        ).actuator_class
        == "pwm"
    )

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Spine unit tests that need no NATS at all.

test_spine.py is integration-only (``pytestmark = requires_nats``, a real
JetStream) by design — see its module docstring. These exercise the parts of
``Spine`` that are pure Python: the ``STREAMS`` config tuple, and
``publish_chip_state``'s subject-sanitization and unconnected-RuntimeError
behaviour, against a small fake stand-in for the underlying ``nc`` client
rather than a broker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ChipState, subjects
from bellasreef_hardware_io.spine import CHIP_STREAM, STREAMS, Spine
from nats.js.api import RetentionPolicy, StorageType


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeNc:
    """Records every core-pub/sub publish, in place of a real NATS client."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))


def _chip_state(*, instance: str = "1f00098000.pwm") -> ChipState:
    return ChipState(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        hardware_source="pi-pwm",
        instance=instance,
        initialised=True,
        initialised_at=datetime.now(UTC),
        facts={"frequency_hz": 500, "polarity": "normal"},
    )


def test_br_chip_stream_is_configured_as_retained_last_value() -> None:
    """Mirrors BR_CAPABILITY's shape: last-value-per-subject, on file."""
    by_name = {config.name: config for config in STREAMS}
    assert CHIP_STREAM == "BR_CHIP"
    chip = by_name[CHIP_STREAM]
    assert chip.subjects == [subjects.ALL_CHIPS]
    assert chip.retention == RetentionPolicy.LIMITS
    assert chip.storage == StorageType.FILE
    assert chip.max_msgs_per_subject == 1


def test_publish_chip_state_uses_the_sanitized_subject() -> None:
    """The '.' in a PWM instance id must not split the subject token."""

    async def scenario() -> tuple[str, bytes, ChipState]:
        spine = Spine("nats://example.invalid:4222")
        fake_nc = _FakeNc()
        spine._nc = fake_nc  # type: ignore[assignment]
        state = _chip_state(instance="1f00098000.pwm")
        await spine.publish_chip_state(state)
        subject, payload = fake_nc.published[0]
        return subject, payload, state

    subject, payload, state = run(scenario)

    assert subject == "bellasreef.chip.pi-pwm.1f00098000-pwm"
    # The payload round-trips back to the exact model that was published —
    # including message_id and the two datetimes, which a UUID- or
    # datetime-serialization bug would otherwise slip past unnoticed.
    assert ChipState.model_validate_json(payload) == state


def test_publish_chip_state_without_a_connection_raises() -> None:
    """Same contract as every other core-pub/sub publish on this class."""

    async def scenario() -> None:
        spine = Spine("nats://example.invalid:4222")
        await spine.publish_chip_state(_chip_state())

    with pytest.raises(RuntimeError, match="spine not connected"):
        run(scenario)

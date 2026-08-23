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
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_contracts import ChipState, Heartbeat, subjects
from bellasreef_hardware_io import InterlockSupervisor, SafetyEvent
from bellasreef_hardware_io.spine import CHIP_STREAM, STREAMS, CommandConsumer, Spine
from nats.js.api import RetentionPolicy, StorageType


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class _FakeMsg:
    """Duck-types nats.aio.msg.Msg's ``.data``/``.subject`` — the whole
    surface subscribe_heartbeats reads from a delivered message."""

    def __init__(self, payload: bytes, subject: str) -> None:
        self.data = payload
        self.subject = subject


class _FakeNc:
    """Records every core-pub/sub publish, in place of a real NATS client.

    ``subscribe`` also captures the callback per subject so a test can drive
    it directly with a ``_FakeMsg``, the same way test_assignments.py's
    ``_FakeNc`` does for the control-engine side of this same pattern.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: dict[str, Callable[[_FakeMsg], Coroutine[Any, Any, None]]] = {}

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def subscribe(
        self, subject: str, cb: Callable[[_FakeMsg], Coroutine[Any, Any, None]]
    ) -> None:
        self.subscriptions[subject] = cb


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


def _heartbeat(*, sequence: int = 1) -> Heartbeat:
    return Heartbeat(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="control-engine",
        component="control-engine",
        sequence=sequence,
        interval_s=1.0,
    )


def test_heartbeat_subscription_feeds_supervisor() -> None:
    """Real payloads through the real parse path, mirroring watch_assignments'
    malformed-payload contract: a bad beat is dropped with a warning and the
    subscription stays alive for the next one.

    Asserted after each delivery, not just at the end, so a future failure
    names which delivery misbehaved rather than just the final tally.
    """

    async def scenario() -> None:
        spine = Spine("nats://example.invalid:4222")
        fake_nc = _FakeNc()
        spine._nc = fake_nc  # type: ignore[assignment]

        beats: list[int] = []
        await spine.subscribe_heartbeats("control-engine", lambda: beats.append(1))

        subject = subjects.heartbeat("control-engine")
        cb = fake_nc.subscriptions[subject]

        await cb(_FakeMsg(_heartbeat(sequence=1).model_dump_json().encode(), subject))
        assert beats == [1], "a valid beat must feed the supervisor"

        await cb(_FakeMsg(b"not json", subject))
        assert beats == [1], "a malformed beat must be dropped, not counted"

        await cb(_FakeMsg(_heartbeat(sequence=2).model_dump_json().encode(), subject))
        assert beats == [1, 1], "the subscription must survive the malformed beat"

    run(scenario)


def test_heartbeat_subscription_survives_a_raising_handler() -> None:
    """A raising on_beat must not escape the callback and kill the
    subscription — the failure mode the parsing/handling split guards
    against (control-engine's subscribe_assignments has the same test)."""

    async def scenario() -> None:
        spine = Spine("nats://example.invalid:4222")
        fake_nc = _FakeNc()
        spine._nc = fake_nc  # type: ignore[assignment]

        def on_beat() -> None:
            raise RuntimeError("boom")

        await spine.subscribe_heartbeats("control-engine", on_beat)

        subject = subjects.heartbeat("control-engine")
        cb = fake_nc.subscriptions[subject]
        await cb(_FakeMsg(_heartbeat().model_dump_json().encode(), subject))  # must not raise

    run(scenario)


def test_subscribe_heartbeats_without_a_connection_raises() -> None:
    """Same contract as every other spine surface on an unconnected client."""

    async def scenario() -> None:
        spine = Spine("nats://example.invalid:4222")
        await spine.subscribe_heartbeats("control-engine", lambda: None)

    with pytest.raises(RuntimeError, match="spine not connected"):
        run(scenario)


class _FakeJs:
    """Records every publish, in place of a real JetStream context — the
    ``spine.js`` property surface ``CommandConsumer._audit`` touches."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str] | None]] = []

    async def publish(
        self, subject: str, payload: bytes, *, headers: dict[str, str] | None = None
    ) -> None:
        self.published.append((subject, payload, headers))


async def _noop_on_event(event: SafetyEvent) -> None:
    pass


def _consumer(spine: Spine) -> CommandConsumer:
    supervisor = InterlockSupervisor(on_event=_noop_on_event)
    return CommandConsumer(spine, supervisor)


def test_audit_stamps_the_hardware_io_actor() -> None:
    """Finding 8 (2026-08-23 review): the merged audit dict carried no
    ``actor``, so the writer's fallback default attributed every hardware-io
    refusal to whichever service happened to be its guess — never
    hardware-io itself, which is the one that actually refused it."""

    async def scenario() -> dict[str, object]:
        spine = Spine("nats://example.invalid:4222")
        fake_js = _FakeJs()
        spine._js = fake_js  # type: ignore[assignment]
        consumer = _consumer(spine)

        await consumer._audit("command_refused", {"actuator_id": "ato-pump"})

        _subject, payload, _headers = fake_js.published[0]
        return dict(json.loads(payload))

    event = run(scenario)
    assert event["actor"] == "hardware-io"
    assert event["event"] == "command_refused"
    assert event["actuator_id"] == "ato-pump"


def test_audit_lets_an_explicit_actor_in_detail_win() -> None:
    """The merge order (``detail`` spread last) is the honest one: nothing
    sets ``actor`` in ``detail`` today, but if something ever did, it must
    not be silently overwritten by the constant."""

    async def scenario() -> dict[str, object]:
        spine = Spine("nats://example.invalid:4222")
        fake_js = _FakeJs()
        spine._js = fake_js  # type: ignore[assignment]
        consumer = _consumer(spine)

        await consumer._audit("malformed_command", {"actor": "someone-else"})

        _subject, payload, _headers = fake_js.published[0]
        return dict(json.loads(payload))

    event = run(scenario)
    assert event["actor"] == "someone-else"

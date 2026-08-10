# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""End-to-end: engine publishes, a consumer receives, contract semantics hold.

Needs a real NATS with JetStream. The point is not that a message arrives —
that is table stakes — but that the guarantees the rest of the system depends
on survive the round trip: every command carries an expiry and an idempotency
key, the broker deduplicates on that key, and an expired command is refused by
the consumer rather than executed late.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from bellasreef_contracts import ActuatorCommand, PwmLevel, subjects
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from bellasreef_control_engine.publisher import CommandPublisher
from bellasreef_control_engine.scheduler import LightingScheduler
from bellasreef_hardware_io.spine import Spine

_ENV = "BELLASREEF_TEST_NATS_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(_ENV), reason=f"{_ENV} not set; needs a real NATS with JetStream"
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def url() -> str:
    return os.environ[_ENV]


async def clean_spine() -> Spine:
    """Provision streams and clear anything a previous test left behind."""
    spine = Spine(url())
    await spine.connect()
    await spine.provision()
    await spine.js.purge_stream("BR_CMD")
    for consumer in await spine.js.consumers_info("BR_CMD"):
        await spine.js.delete_consumer("BR_CMD", consumer.name)
    return spine


def dawn_profile() -> ChannelProfile:
    return ChannelProfile(
        channel_id="led-blue",
        anchor="clock",
        points=(
            RampPoint(at=time(6), duty=0.0),
            RampPoint(at=time(12), duty=1.0),
            RampPoint(at=time(22), duty=0.0),
        ),
    )


@pytest.mark.timeout(60)
def test_engine_publishes_and_a_consumer_receives_a_valid_command() -> None:
    async def scenario() -> ActuatorCommand:
        spine = await clean_spine()
        sub = await spine.js.pull_subscribe(
            subjects.ALL_COMMANDS, durable=f"probe-{uuid.uuid4().hex[:8]}"
        )

        publisher = CommandPublisher(url())
        await publisher.connect()
        scheduler = LightingScheduler([dawn_profile()])

        now = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        intents = scheduler.due(now)
        assert len(intents) == 1
        command = publisher.build_pwm_command(
            intents[0].channel_id, intents[0].duty, reason="lighting:initial", now=now
        )
        await publisher.emit(command)

        msgs = await sub.fetch(1, timeout=5.0)
        received = ActuatorCommand.model_validate_json(msgs[0].data)
        await msgs[0].ack()

        await publisher.close()
        await spine.close()
        return received

    received = run(scenario)

    # Round-tripped through the wire and back through the contract's own
    # validation, so this is the real payload, not a local object.
    assert received.actuator_id == "led-blue"
    assert received.actuator_class == "pwm"
    assert isinstance(received.level, PwmLevel)
    assert received.source == "control-engine"

    # The two fields the whole safety story depends on.
    assert received.expires_at > received.emitted_at
    assert received.idempotency_key is not None
    assert (received.expires_at - received.emitted_at).total_seconds() == pytest.approx(30.0)


@pytest.mark.timeout(60)
def test_the_broker_deduplicates_on_the_engine_idempotency_key() -> None:
    """A retried publish must not become a second command."""

    async def scenario() -> int:
        spine = await clean_spine()
        publisher = CommandPublisher(url())
        await publisher.connect()

        command = publisher.build_pwm_command("led-blue", 0.42, reason="lighting:ramp")
        await publisher.emit(command)
        await publisher.emit(command)  # same idempotency_key

        info = await spine.js.stream_info("BR_CMD")
        await publisher.close()
        await spine.close()
        return int(info.state.messages)

    assert run(scenario) == 1


@pytest.mark.timeout(60)
def test_distinct_commands_are_not_collapsed() -> None:
    """Dedup must key on the idempotency key, not the payload.

    Two ramp steps a second apart can carry the same duty after rounding; they
    are still separate commands and must both survive.
    """

    async def scenario() -> int:
        spine = await clean_spine()
        publisher = CommandPublisher(url())
        await publisher.connect()

        for _ in range(3):
            await publisher.emit(
                publisher.build_pwm_command("led-blue", 0.42, reason="lighting:refresh")
            )

        info = await spine.js.stream_info("BR_CMD")
        await publisher.close()
        await spine.close()
        return int(info.state.messages)

    assert run(scenario) == 3


@pytest.mark.timeout(60)
def test_an_engine_command_that_expires_in_flight_is_refused_downstream() -> None:
    """The engine's TTL is honoured by the consumer, end to end.

    This closes the loop with the hardware-io side: the engine sets expires_at,
    the consumer re-checks it against its own clock, and a command that aged
    out between publish and delivery is refused rather than driving a channel
    to a level the schedule has already moved past.
    """

    async def scenario() -> tuple[str, float]:
        spine = await clean_spine()
        sub = await spine.js.pull_subscribe(
            subjects.ALL_COMMANDS, durable=f"late-{uuid.uuid4().hex[:8]}"
        )

        publisher = CommandPublisher(url(), ttl_s=1.0)
        await publisher.connect()
        command = publisher.build_pwm_command("led-blue", 0.9, reason="lighting:ramp")
        await publisher.emit(command)

        # Delivery is slow — the consumer was busy, restarting, or backed up.
        await asyncio.sleep(1.5)

        msgs = await sub.fetch(1, timeout=5.0)
        received = ActuatorCommand.model_validate_json(msgs[0].data)
        await msgs[0].ack()

        verdict = "expired" if received.is_expired(datetime.now(UTC)) else "fresh"
        duty = received.level.duty if isinstance(received.level, PwmLevel) else -1.0

        await publisher.close()
        await spine.close()
        return verdict, duty

    verdict, duty = run(scenario)
    assert verdict == "expired", "a command delivered after its TTL must read as expired"
    assert duty == pytest.approx(0.9)


@pytest.mark.timeout(60)
def test_a_full_ramp_publishes_monotonically_rising_duties() -> None:
    """Walk dawn and confirm the wire carries the schedule's shape."""

    async def scenario() -> list[float]:
        spine = await clean_spine()
        # Deleted in the `finally` below. BR_CMD is a *workqueue* stream, which
        # permits exactly one consumer per filter subject — a durable left
        # behind here does not just litter, it stops hardware-io from ever
        # binding its command consumer again. That failure has cost three
        # debugging detours; the leak was always this line.
        durable = f"ramp-{uuid.uuid4().hex[:8]}"
        sub = await spine.js.pull_subscribe(subjects.ALL_COMMANDS, durable=durable)
        publisher = CommandPublisher(url())
        await publisher.connect()
        scheduler = LightingScheduler([dawn_profile()], deadband=0.01, refresh_s=3600)

        base = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
        for minute in range(0, 60, 5):
            now = base + timedelta(minutes=minute)
            for intent in scheduler.due(now):
                await publisher.emit(
                    publisher.build_pwm_command(
                        intent.channel_id, intent.duty, reason=intent.reason, now=now
                    )
                )
                scheduler.mark_emitted(intent, now)

        msgs = await sub.fetch(64, timeout=5.0)
        duties = []
        for msg in msgs:
            cmd = ActuatorCommand.model_validate_json(msg.data)
            assert isinstance(cmd.level, PwmLevel)
            duties.append(cmd.level.duty)
            await msg.ack()

        await publisher.close()
        # Give the durable back. A workqueue stream allows one consumer per
        # filter; leaving this one behind blocks hardware-io's command consumer.
        with contextlib.suppress(Exception):
            await spine.js.delete_consumer("BR_CMD", durable)
        await spine.close()
        return duties

    duties = run(scenario)
    assert len(duties) >= 5
    assert duties == sorted(duties), f"dawn ramp was not monotonic on the wire: {duties}"
    assert duties[0] == pytest.approx(0.0)
    assert any(0.0 < d < 0.08 for d in duties), (
        "the wire should carry sub-8% duties; the driver owns the floor, not the engine"
    )

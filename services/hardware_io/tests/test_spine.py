# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Spine integration tests. Require a real NATS with JetStream.

Skipped unless ``BELLASREEF_TEST_NATS_URL`` is set. CI sets it against a
``nats:2.10-alpine`` service container.

The headline test here is the redelivered-but-expired command. It is the
wire-level twin of the supervisor's own expiry check, and the two are not
redundant: the supervisor guards the command it is handed, this guards what the
broker chooses to hand it. A consumer that trusts delivery to imply freshness
passes every unit test and still tops off a tank an hour late.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bellasreef_contracts import ActuatorCommand, ActuatorRegistration, BinaryLevel
from bellasreef_hardware_io import FakeActuator, InterlockSupervisor, SafetyEvent
from bellasreef_hardware_io.spine import CommandConsumer, Spine

_ENV = "BELLASREEF_TEST_NATS_URL"

requires_nats = pytest.mark.skipif(
    not os.environ.get(_ENV),
    reason=f"{_ENV} not set; these need a real NATS with JetStream",
)

pytestmark = requires_nats

OFF = BinaryLevel(on=False)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def nats_url() -> str:
    url = os.environ.get(_ENV)
    if not url:
        pytest.skip(f"{_ENV} not set")
    return url


def registration(actuator_id: str) -> ActuatorRegistration:
    return ActuatorRegistration(
        message_id=uuid.uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=actuator_id,
        actuator_class="binary",
        role="outlet",
        driver_id="fake-actuator",
        safe_state=OFF,
        max_runtime_s=3600.0,
        heartbeat_timeout_s=30.0,
    )


def command(actuator_id: str, *, ttl_s: float, on: bool = True) -> ActuatorCommand:
    now = datetime.now(UTC)
    return ActuatorCommand(
        message_id=uuid.uuid4(),
        emitted_at=now,
        source="control-engine",
        actuator_id=actuator_id,
        actuator_class="binary",
        level=BinaryLevel(on=on),
        idempotency_key=uuid.uuid4(),
        expires_at=now + timedelta(seconds=ttl_s),
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[SafetyEvent] = []

    async def __call__(self, event: SafetyEvent) -> None:
        self.events.append(event)

    def reasons(self) -> list[str]:
        return [e.reason for e in self.events]


async def fresh_spine() -> Spine:
    spine = Spine(nats_url())
    await spine.connect()
    await spine.provision()
    # Each test starts from an empty command stream; leftovers from a previous
    # test would show up as phantom redeliveries.
    await spine.js.purge_stream("BR_CMD")
    await spine.js.purge_stream("BR_AUDIT")

    # A workqueue stream permits exactly ONE consumer per filter subject, so a
    # durable left behind by an earlier test makes the next one fail with
    # "filtered consumer not unique on workqueue stream". Production has a
    # single `hardware-io` consumer and never hits this; the test harness has
    # to clean up after itself.
    for stream in ("BR_CMD", "BR_AUDIT"):
        for consumer in await spine.js.consumers_info(stream):
            await spine.js.delete_consumer(stream, consumer.name)
    return spine


# ---------------------------------------------------------------- amendment A


@pytest.mark.timeout(60)
def test_redelivered_but_expired_command_is_dropped_and_audited() -> None:
    """A command that expires between delivery and redelivery must NOT execute.

    Scenario, which is an ordinary crash and not an exotic one:

      t=0.0  command published with a 1 s TTL, delivered, consumer dies before
             acking
      t=1.0  the command expires
      t=2.0  ack_wait elapses, JetStream redelivers it

    At t=2.0 the reason the command was issued is an hour stale in tank terms.
    Executing it is worse than dropping it: the ATO level that justified a
    top-off may already have been corrected.
    """

    async def scenario() -> tuple[list[str], bool, list[str], int, int]:
        spine = await fresh_spine()
        rec = Recorder()
        supervisor = InterlockSupervisor(on_event=rec)
        actuator = FakeActuator("ato-pump", OFF)
        supervisor.register(registration("ato-pump"), actuator)
        await supervisor.start()
        supervisor.heartbeat()

        consumer = CommandConsumer(
            spine,
            supervisor,
            durable=f"drill-{uuid.uuid4().hex[:8]}",
            ack_wait_s=2.0,
            max_deliver=3,
        )
        await consumer.subscribe()

        await spine.publish_command(command("ato-pump", ttl_s=1.0))

        # First delivery, then the process "dies" before acking. Using the
        # subscription directly is the point: this is a crash, not an API.
        assert consumer._sub is not None
        first = await consumer._sub.fetch(1, timeout=5.0)
        assert len(first) == 1, "command was not delivered the first time"

        # Past both the TTL and ack_wait, so the redelivery is expired.
        await asyncio.sleep(2.5)

        outcomes = await consumer.drain_once(timeout=5.0)

        safe = actuator.is_safe()
        reasons = rec.reasons()

        # The wire-level assertions. The supervisor already refuses expired
        # commands, so "it did not execute" would pass even with an empty
        # consumer — these are the parts only the consumer can get wrong.
        audit_info = await spine.js.stream_info("BR_AUDIT")
        audited = int(audit_info.state.messages)

        cmd_info = await spine.js.stream_info("BR_CMD")
        left_pending = int(cmd_info.state.messages)

        await supervisor.stop()
        await spine.close()
        return [str(o) for o in outcomes], safe, reasons, audited, left_pending

    outcomes, safe, reasons, audited, left_pending = run(scenario)

    # 1. Not executed. Guaranteed by the supervisor, asserted here for the record.
    assert outcomes == ["rejected_expired"], (
        f"expected the redelivery to be refused, got {outcomes}"
    )
    assert safe, "an expired command actuated the pump"
    assert "command_expired" in reasons

    # 2. The drop reached the audit stream. An expired dose that vanishes
    #    without a trace is indistinguishable from one that never existed, and
    #    post-incident that is the difference between a diagnosis and a shrug.
    assert audited >= 1, (
        "the expired command was dropped but never audited to the spine — "
        "in-process logging is not the audit trail"
    )

    # 3. Terminated, not left to burn the max_deliver budget. An expired
    #    command that keeps redelivering is pure noise on a work queue.
    assert left_pending == 0, (
        f"{left_pending} expired command(s) still on BR_CMD; it will redeliver"
    )


# ---------------------------------------------------------------- other paths


@pytest.mark.timeout(60)
def test_fresh_command_is_applied() -> None:
    """The control case — expiry checking must not refuse valid work."""

    async def scenario() -> tuple[list[str], bool]:
        spine = await fresh_spine()
        supervisor = InterlockSupervisor(on_event=Recorder())
        actuator = FakeActuator("ato-pump", OFF)
        supervisor.register(registration("ato-pump"), actuator)
        await supervisor.start()
        supervisor.heartbeat()

        consumer = CommandConsumer(spine, supervisor, durable=f"ok-{uuid.uuid4().hex[:8]}")
        await consumer.subscribe()
        await spine.publish_command(command("ato-pump", ttl_s=60.0))

        outcomes = await consumer.drain_once(timeout=5.0)
        on = not actuator.is_safe()
        await supervisor.stop()
        await spine.close()
        return [str(o) for o in outcomes], on

    outcomes, turned_on = run(scenario)
    assert outcomes == ["applied"]
    assert turned_on


@pytest.mark.timeout(60)
def test_duplicate_msg_id_is_deduplicated_by_the_broker() -> None:
    """Nats-Msg-Id + duplicate_window: publishing twice must store once."""

    async def scenario() -> int:
        spine = await fresh_spine()
        cmd = command("ato-pump", ttl_s=60.0)
        await spine.publish_command(cmd)
        await spine.publish_command(cmd)  # identical idempotency_key
        info = await spine.js.stream_info("BR_CMD")
        await spine.close()
        return int(info.state.messages)

    assert run(scenario) == 1, "the broker stored a duplicate command"


@pytest.mark.timeout(60)
def test_streams_are_provisioned_to_the_documented_shape() -> None:
    """The stream config is contract, not incidental."""

    async def scenario() -> dict[str, Any]:
        spine = await fresh_spine()
        cmd = await spine.js.stream_info("BR_CMD")
        state = await spine.js.stream_info("BR_STATE")
        audit = await spine.js.stream_info("BR_AUDIT")
        await spine.close()
        return {
            "cmd_retention": cmd.config.retention,
            "cmd_dupe_window": cmd.config.duplicate_window,
            "state_retention": state.config.retention,
            "state_per_subject": state.config.max_msgs_per_subject,
            "audit_retention": audit.config.retention,
            "audit_max_age": audit.config.max_age,
        }

    got = run(scenario)

    # stream_info returns policies as plain strings, not the enums used on the
    # way in — normalise rather than assume symmetry.
    def policy(v: object) -> str:
        return str(getattr(v, "value", v))

    assert policy(got["cmd_retention"]) == "workqueue"
    assert got["cmd_dupe_window"] == pytest.approx(300.0)
    assert policy(got["state_retention"]) == "limits"
    assert got["state_per_subject"] == 1
    assert policy(got["audit_retention"]) == "workqueue"
    assert got["audit_max_age"] == pytest.approx(604800.0)


@pytest.mark.timeout(60)
def test_state_stream_keeps_only_the_latest_per_subject() -> None:
    """A restarting service should see current state, not replay history."""

    async def scenario() -> int:
        spine = await fresh_spine()
        await spine.js.purge_stream("BR_STATE")
        from bellasreef_contracts import ActuatorState

        for i in range(5):
            await spine.publish_state(
                ActuatorState(
                    message_id=uuid.uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="hardware-io",
                    actuator_id="ato-pump",
                    level=BinaryLevel(on=bool(i % 2)),
                    reason="commanded",
                    since=datetime.now(UTC),
                )
            )
        info = await spine.js.stream_info("BR_STATE")
        await spine.close()
        return int(info.state.messages)

    assert run(scenario) == 1

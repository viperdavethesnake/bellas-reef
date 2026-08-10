# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""NATS spine.

Stream and consumer provisioning plus the command path, against the real
`nats.js` API in nats-py 2.15 — floats in seconds, policy enums, not the
`nats.jetstream` package that does not exist here.

Layout follows docs/contracts/nats-subjects.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

import nats
from bellasreef_contracts import (
    ActuatorCommand,
    ActuatorRegistration,
    ActuatorState,
    Heartbeat,
    subjects,
)
from nats.aio.client import Client
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import BadRequestError
from pydantic import ValidationError

from bellasreef_hardware_io.logging import get_logger
from bellasreef_hardware_io.safety import CommandOutcome, InterlockSupervisor

__all__ = ["STREAMS", "CommandConsumer", "Spine"]

log = get_logger(__name__)

CMD_STREAM: Final = "BR_CMD"
STATE_STREAM: Final = "BR_STATE"
AUDIT_STREAM: Final = "BR_AUDIT"

STREAMS: Final = (
    StreamConfig(
        name=CMD_STREAM,
        subjects=[subjects.ALL_COMMANDS],
        retention=RetentionPolicy.WORK_QUEUE,
        storage=StorageType.FILE,
        discard=DiscardPolicy.OLD,
        max_age=3600.0,
        duplicate_window=300.0,
    ),
    StreamConfig(
        name=STATE_STREAM,
        subjects=[subjects.ALL_STATE],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_msgs_per_subject=1,
    ),
    StreamConfig(
        name=AUDIT_STREAM,
        subjects=[subjects.ALL_AUDIT],
        retention=RetentionPolicy.WORK_QUEUE,
        storage=StorageType.FILE,
        max_age=604800.0,
    ),
)


class Spine:
    """Connection, provisioning, and publishing."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._nc: Client | None = None
        self._js: JetStreamContext | None = None

    @property
    def js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("spine not connected")
        return self._js

    async def connect(self) -> None:
        self._nc = await nats.connect(self._url)
        self._js = self._nc.jetstream()
        log.info("spine connected", extra={"url": self._url})

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._js = None

    async def provision(self) -> None:
        """Create streams, or update them if the config has moved on."""
        for config in STREAMS:
            try:
                await self.js.add_stream(config)
                log.info("stream created", extra={"stream": config.name})
            except BadRequestError:
                await self.js.update_stream(config)
                log.info("stream updated", extra={"stream": config.name})

    # ------------------------------------------------------------- publishing

    async def publish_command(self, command: ActuatorCommand) -> None:
        """Publish with Nats-Msg-Id so JetStream de-duplicates redelivery."""
        await self.js.publish(
            subjects.cmd(command.actuator_class, command.actuator_id),
            command.model_dump_json().encode(),
            headers={"Nats-Msg-Id": str(command.idempotency_key)},
        )

    async def publish_state(self, state: ActuatorState) -> None:
        await self.js.publish(subjects.state(state.actuator_id), state.model_dump_json().encode())

    async def publish_heartbeat(self, beat: Heartbeat) -> None:
        """Core pub/sub, never JetStream.

        A replayed heartbeat would make a dead controller look alive.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")
        await self._nc.publish(subjects.heartbeat(beat.component), beat.model_dump_json().encode())

    async def publish_registration(self, registration: ActuatorRegistration) -> None:
        if self._nc is None:
            raise RuntimeError("spine not connected")
        await self._nc.publish(
            subjects.registry(registration.actuator_id),
            registration.model_dump_json().encode(),
        )

    async def publish_audit(self, category: str, event: dict[str, object]) -> None:
        """Publish an audit event, stamped with an id the store dedups on.

        The same id goes in the payload and the ``Nats-Msg-Id`` header: the
        header lets the broker drop an accidental republish inside the
        duplicate window, and the payload id lets Postgres reject a redelivery
        after it. Delivery is at-least-once, so the terminal store is where
        exactly-once actually gets decided.
        """
        message_id = event.get("message_id") or str(uuid4())
        payload = {**event, "message_id": str(message_id)}
        await self.js.publish(
            subjects.audit(category),
            json.dumps(payload).encode(),
            headers={"Nats-Msg-Id": str(message_id)},
        )


class CommandConsumer:
    """Applies commands from BR_CMD to the supervisor.

    The supervisor is the authority on whether a command may execute — it holds
    the interlocks, and it refuses expired, latched and untrusted-clock
    commands. That means "an expired redelivery never actuates" is already true
    before this class does anything.

    What this class is responsible for is everything *around* that decision:

    * a refusal must reach the **audit stream**. An expired dose that vanishes
      without a trace is indistinguishable from one that never existed, and
      post-incident that is the difference between a diagnosis and a shrug.
    * a refused message must be **terminated**, not acked-and-forgotten or left
      to redeliver. It will never succeed on retry, so saying so is more honest
      than letting it burn the ``max_deliver`` budget.
    """

    def __init__(
        self,
        spine: Spine,
        supervisor: InterlockSupervisor,
        *,
        durable: str = "hardware-io",
        ack_wait_s: float = 5.0,
        max_deliver: int = 3,
    ) -> None:
        self._spine = spine
        self._supervisor = supervisor
        self._durable = durable
        self._ack_wait_s = ack_wait_s
        self._max_deliver = max_deliver
        self._sub: JetStreamContext.PullSubscription | None = None

    async def subscribe(self) -> None:
        self._sub = await self._spine.js.pull_subscribe(
            subjects.ALL_COMMANDS,
            durable=self._durable,
            config=ConsumerConfig(
                durable_name=self._durable,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=self._ack_wait_s,
                max_deliver=self._max_deliver,
            ),
        )

    async def drain_once(self, *, batch: int = 16, timeout: float = 1.0) -> list[CommandOutcome]:
        """Fetch and process one batch. Returns what happened to each message."""
        if self._sub is None:
            raise RuntimeError("not subscribed")
        try:
            msgs = await self._sub.fetch(batch, timeout=timeout)
        except (TimeoutError, nats.errors.TimeoutError):
            return []

        outcomes: list[CommandOutcome] = []
        for msg in msgs:
            try:
                command = ActuatorCommand.model_validate_json(msg.data)
            except ValidationError as exc:
                # A payload that does not satisfy the contract will not start
                # satisfying it on redelivery. Terminate and record it.
                await self._audit(
                    "malformed_command",
                    {"subject": msg.subject, "error": str(exc)},
                )
                await msg.term()
                continue

            outcome = await self._supervisor.apply(command)
            outcomes.append(outcome)

            if outcome == "applied":
                await msg.ack()
                continue

            await self._audit(
                "command_refused",
                {
                    "outcome": outcome,
                    "actuator_id": command.actuator_id,
                    "message_id": str(command.message_id),
                    "idempotency_key": str(command.idempotency_key),
                    "expires_at": command.expires_at.isoformat(),
                    "observed_at": utcnow().isoformat(),
                    "delivered": msg.metadata.num_delivered,
                },
            )
            # term(), not ack(): this command will never succeed on retry, and
            # saying so is more useful than silently consuming a delivery.
            await msg.term()

        return outcomes

    async def _audit(self, event_type: str, detail: dict[str, object]) -> None:
        await self._spine.publish_audit("command", {"event": event_type, **detail})


def utcnow() -> datetime:
    return datetime.now(UTC)

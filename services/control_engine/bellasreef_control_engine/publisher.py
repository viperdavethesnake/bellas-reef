# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The sole command publisher (PRD §7.1).

Every actuator command in the system leaves through :meth:`CommandPublisher.emit`
and nowhere else. That is not tidiness — it is what makes several later things
possible in one place instead of many:

* **Shadow mode (R3, next pass)** becomes a branch here. The scheduler already
  yields intents rather than side effects, so shadow mode is "journal the
  intent and return" at this one method. If commands were published from inside
  the scheduler, R3 would mean touching every control module instead.
* **Command expiry** is set here from one policy, so no control module can
  quietly issue something longer-lived than the rest.
* **Idempotency keys** are minted here, so every command has one by
  construction rather than by everyone remembering.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import nats
from bellasreef_contracts import (
    ActuatorCommand,
    DeviceAssignment,
    PwmLevel,
    SensorAlert,
    SensorReading,
    SensorSilence,
    subjects,
)
from bellasreef_service import get_logger
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError
from pydantic import ValidationError

from bellasreef_control_engine.assignments import AssignmentLedger

__all__ = ["DEFAULT_COMMAND_TTL_S", "CommandPublisher"]

log = get_logger(__name__)

#: How long a lighting command stays valid.
#:
#: Long enough to survive a brief spine hiccup, short enough that a command
#: delivered after it is meaningless. A ramp step is superseded within seconds
#: anyway, so executing a stale one would drive a channel to a level the
#: schedule has already moved past.
DEFAULT_COMMAND_TTL_S = 30.0


class CommandPublisher:
    """Publishes to `bellasreef.cmd.*` and heartbeats to `bellasreef.heartbeat.*`."""

    def __init__(
        self,
        url: str,
        *,
        source: str = "control-engine",
        ttl_s: float = DEFAULT_COMMAND_TTL_S,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        self._url = url
        self._source = source
        self._ttl_s = ttl_s
        self._nc: Client | None = None
        self._js: JetStreamContext | None = None
        self._beat_seq = 0

    async def connect(self) -> None:
        self._nc = await nats.connect(self._url)
        self._js = self._nc.jetstream()
        log.info("publisher connected", extra={"url": self._url})

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._js = None

    @property
    def connected(self) -> bool:
        return self._js is not None

    def build_pwm_command(
        self, channel_id: str, duty: float, *, reason: str, now: datetime | None = None
    ) -> ActuatorCommand:
        """Mint a command. Every one carries an idempotency key and an expiry.

        Separated from :meth:`emit` so tests can inspect what would be sent
        without a broker — and so shadow mode can journal the exact command it
        declined to publish, rather than a description of one.
        """
        issued = now or datetime.now(UTC)
        return ActuatorCommand(
            message_id=uuid4(),
            emitted_at=issued,
            source=self._source,
            actuator_id=channel_id,
            actuator_class="pwm",
            level=PwmLevel(duty=duty),
            idempotency_key=uuid4(),
            expires_at=issued + timedelta(seconds=self._ttl_s),
            reason=reason,
        )

    async def emit(self, command: ActuatorCommand) -> None:
        """The one place a command leaves this service."""
        if self._js is None:
            raise RuntimeError("publisher not connected")
        await self._js.publish(
            subjects.cmd(command.actuator_class, command.actuator_id),
            command.model_dump_json().encode(),
            headers={"Nats-Msg-Id": str(command.idempotency_key)},
        )
        log.info(
            "command published",
            extra={
                "actuator_id": command.actuator_id,
                "duty": command.level.duty if isinstance(command.level, PwmLevel) else None,
                "reason": command.reason,
                "expires_at": command.expires_at.isoformat(),
            },
        )

    async def publish_alert(self, subject: str, alert: SensorAlert) -> None:
        """Alerts go out on core pub/sub, not JetStream.

        Same reasoning as telemetry: an alert describes the tank *now*. A
        durable queue would hand a client that was offline overnight a backlog
        of breaches that have long since cleared, and the audit log — which is
        durable — is where the history actually belongs.
        """
        if self._nc is None:
            raise RuntimeError("publisher not connected")
        await self._nc.publish(subject, alert.model_dump_json().encode())

    async def publish_silence(self, subject: str, message: SensorSilence) -> None:
        """A probe going quiet, on core pub/sub for the same reason as alerts.

        Its own subject root, not a token under ``bellasreef.alert.``, so that
        subscribers parsing every message there as a SensorAlert are not handed
        a payload they are obliged to reject.
        """
        if self._nc is None:
            raise RuntimeError("publisher not connected")
        await self._nc.publish(subject, message.model_dump_json().encode())

    async def publish_audit(self, category: str, event: dict[str, object]) -> None:
        """Durable audit event, deduped on the same id in the header and payload."""
        if self._js is None:
            raise RuntimeError("publisher not connected")
        message_id = event.get("message_id") or str(uuid4())
        payload = {**event, "message_id": str(message_id)}
        await self._js.publish(
            subjects.audit(category),
            json.dumps(payload).encode(),
            headers={"Nats-Msg-Id": str(message_id)},
        )

    async def subscribe_sensors(self, handler: Callable[[SensorReading], Awaitable[None]]) -> None:
        """Feed every sensor reading to ``handler``.

        A malformed payload is logged and dropped rather than raised: this
        callback runs on the NATS client's own task, and letting an exception
        escape it kills the subscription silently — the engine would keep
        running, keep reporting healthy, and never evaluate another threshold.
        """
        if self._nc is None:
            raise RuntimeError("publisher not connected")

        async def _on_message(msg: Msg) -> None:
            try:
                reading = SensorReading.model_validate_json(msg.data)
            except ValidationError:
                log.warning(
                    "dropping an undecodable sensor reading", extra={"subject": msg.subject}
                )
                return
            try:
                await handler(reading)
            except Exception:  # broad by design - see docstring
                log.exception("alert evaluation failed", extra={"subject": msg.subject})

        await self._nc.subscribe(subjects.ALL_SENSORS, cb=_on_message)
        log.info("subscribed to sensor telemetry", extra={"subject": subjects.ALL_SENSORS})

    async def load_assignments(self, ledger: AssignmentLedger) -> bool:
        """Drain the retained assignment stream into ``ledger``, once.

        Mirrors hardware-io's startup read: LAST_PER_SUBJECT gives the current
        truth per device, tombstones included. Returns False when the stream is
        not provisioned yet — a hub booting in arbitrary order — so the caller
        knows to retry rather than trusting an empty ledger forever.
        """
        if self._js is None:
            raise RuntimeError("publisher not connected")
        try:
            sub = await self._js.pull_subscribe(
                subjects.ALL_ASSIGNMENTS,
                durable=None,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.LAST_PER_SUBJECT),
            )
        except NotFoundError:
            log.warning("assignment stream not provisioned yet; will retry")
            return False

        while True:
            try:
                msgs = await sub.fetch(32, timeout=1.0)
            except (TimeoutError, nats.errors.TimeoutError):
                break
            for msg in msgs:
                try:
                    ledger.apply(DeviceAssignment.model_validate_json(msg.data))
                except ValidationError:
                    log.warning(
                        "assignment did not validate; skipped",
                        extra={"subject": msg.subject},
                    )
                await msg.ack()
        with contextlib.suppress(Exception):
            await sub.unsubscribe()
        log.info("assignments loaded", extra={"adopted": sorted(ledger.adopted)})
        return True

    async def subscribe_assignments(self, handler: Callable[[DeviceAssignment], None]) -> None:
        """Live adoption changes, on core pub/sub.

        A JetStream publish traverses core subjects too, so a plain subscription
        hears every bind/unbind the API publishes — no durable, deliberately:
        a durable here would contend with nothing but would still be broker
        state to leak. Malformed payloads are dropped with a log, same contract
        as subscribe_sensors: parsing and handling are guarded separately, so a
        handler that raises cannot escape this callback and kill the
        subscription silently.
        """
        if self._nc is None:
            raise RuntimeError("publisher not connected")

        async def _on_message(msg: Msg) -> None:
            try:
                assignment = DeviceAssignment.model_validate_json(msg.data)
            except ValidationError:
                log.warning("dropping an undecodable assignment", extra={"subject": msg.subject})
                return
            try:
                handler(assignment)
            except Exception:  # broad by design - see docstring
                log.exception("assignment handling failed", extra={"subject": msg.subject})

        await self._nc.subscribe(subjects.ALL_ASSIGNMENTS, cb=_on_message)
        log.info("subscribed to assignments", extra={"subject": subjects.ALL_ASSIGNMENTS})

    async def heartbeat(self, interval_s: float) -> None:
        """Core pub/sub, never JetStream.

        hardware-io drives actuators to safe state when these stop arriving, so
        a replayed heartbeat would defeat the mechanism it exists to feed.
        """
        if self._nc is None:
            raise RuntimeError("publisher not connected")
        from bellasreef_contracts import Heartbeat

        self._beat_seq += 1
        beat = Heartbeat(
            message_id=uuid4(),
            emitted_at=datetime.now(UTC),
            source=self._source,
            component=self._source,
            sequence=self._beat_seq,
            interval_s=interval_s,
        )
        await self._nc.publish(subjects.heartbeat(beat.component), beat.model_dump_json().encode())

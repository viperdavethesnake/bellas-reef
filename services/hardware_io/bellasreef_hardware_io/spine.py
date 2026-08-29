# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""NATS spine.

Stream and consumer provisioning plus the command path, against the real
`nats.js` API in nats-py 2.15 — floats in seconds, policy enums, not the
`nats.jetstream` package that does not exist here.

Layout follows docs/contracts/nats-subjects.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

import nats
from bellasreef_contracts import (
    ActuatorCommand,
    ActuatorLevel,
    ActuatorRegistration,
    ActuatorState,
    CapabilityAnnouncement,
    ChipState,
    DeviceAssignment,
    Heartbeat,
    SensorReading,
    SensorRegistration,
    subjects,
)
from bellasreef_service.logging import get_logger
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import BadRequestError, NotFoundError, ServiceUnavailableError
from pydantic import ValidationError

from bellasreef_hardware_io.safety import CommandOutcome, InterlockSupervisor

__all__ = ["STREAMS", "CommandConsumer", "Spine"]

log = get_logger(__name__)

#: ``source`` on every ActuatorState this process publishes. Not imported from
#: app.py's ``SERVICE`` constant — app.py imports from this module, and a
#: back-import would be circular for one shared literal.
SOURCE: Final = "hardware-io"

#: I3: bounds one applied-command state publish, inside the liveness-guarded
#: main loop (CommandConsumer.drain_once runs from HardwareIO._beat_and_serve,
#: on the same loop iteration the 15s liveness guard watches). 2s keeps a
#: hung publish from ever costing more than a fraction of that budget — chosen
#: over Spine.publish_async because it needs no new Spine surface and keeps
#: the same swallow-and-log contract as every other publish here. A batch of
#: many simultaneously-timing-out publishes could still stack past 15s in the
#: worst case; not addressed here; BR_CMD batches are not normally that large.
_PUBLISH_TIMEOUT_S: Final = 2.0

CMD_STREAM: Final = "BR_CMD"
STATE_STREAM: Final = "BR_STATE"
AUDIT_STREAM: Final = "BR_AUDIT"
REGISTRY_STREAM: Final = "BR_REGISTRY"
CAPABILITY_STREAM: Final = "BR_CAPABILITY"
ASSIGNMENT_STREAM: Final = "BR_ASSIGNMENT"
CHIP_STREAM: Final = "BR_CHIP"

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
    # Registrations are announced once, at hardware-io startup. Without
    # retention, a consumer that starts later — or restarts — never learns the
    # hub has any hardware at all, and the devices table stays empty until
    # somebody power-cycles the right service in the right order.
    #
    # Last-value-per-subject, like state: a registration is not an event to
    # replay but a current fact about one device, and the newest one wins.
    StreamConfig(
        name=REGISTRY_STREAM,
        subjects=[subjects.ALL_REGISTRY],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_msgs_per_subject=1,
    ),
    # Capabilities: what this hub's hardware can offer, as opposed to what
    # anyone has decided to do with it. Last-value-per-subject like the
    # registry, so a consumer that starts after hardware-io still learns the
    # topology instead of waiting for the next restart to find out what the
    # hub is made of.
    StreamConfig(
        name=CAPABILITY_STREAM,
        subjects=[subjects.ALL_CAPABILITIES],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_msgs_per_subject=1,
    ),
    # Chip state: how each hardware source instance is configured, as opposed
    # to what it offers (capabilities) or what has been done with it
    # (assignments). Retained last-value per subject, same reasoning as the
    # two above — re-initialisation after a bus fault republishes, and a
    # consumer that starts late still learns the current configuration
    # instead of waiting for the next restart to find out.
    StreamConfig(
        name=CHIP_STREAM,
        subjects=[subjects.ALL_CHIPS],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_msgs_per_subject=1,
    ),
    # Assignments: what the operator has decided each device is bound to.
    # Retained last-value per subject so a hardware-io restarting alone rebuilds
    # every driver, rather than waiting for someone to re-save each device.
    StreamConfig(
        name=ASSIGNMENT_STREAM,
        subjects=[subjects.ALL_ASSIGNMENTS],
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
        #: Called after the underlying NATS client reconnects. Settable any
        #: time before :meth:`connect` runs — that is when it gets wired into
        #: ``reconnected_cb``. Async, unlike control-engine's sibling
        #: (``CommandPublisher.on_reconnected``, publisher.py), because
        #: HardwareIO's handler (``_republish_safe_states``) awaits publishes
        #: rather than just flipping a flag. See :meth:`connect`'s docstring
        #: for why this exists.
        self.on_reconnected: Callable[[], Awaitable[None]] | None = None

    @property
    def js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("spine not connected")
        return self._js

    async def connect(self) -> None:
        """Connect, wiring ``on_reconnected`` into nats.py's ``reconnected_cb``.

        2026-08-23 NATS-outage drill: an outage longer than the heartbeat
        timeout trips actuators to their declared safe state, but the
        trip-state publish fails into the down spine — swallowed by design.
        The engine never learns the trip happened, so its duty memory is
        never corrected, and its first post-recovery command re-energizes the
        dark channel in ONE step at curve duty (observed: 0 -> 11.5% in a
        single ``lighting:ramp`` command at 22:50:57Z; at midday this would
        be 0 -> 100%) instead of slewing up from dark on the path the slew
        exists for. ``on_reconnected`` — HardwareIO wires it to
        ``_republish_safe_states`` before this call — republishes the
        currently-safe actuators once the client is back, correcting the
        engine's memory so recovery slews from dark again.

        The callback body is guarded: a raise out of ``on_reconnected`` must
        not escape into nats.py's own reconnect handling. Same guard shape as
        ``subscribe_heartbeats``'s ``on_beat`` — a bug in the republish path
        must not be able to take down the reconnect it rides on.
        """

        async def _on_reconnected() -> None:
            if self.on_reconnected is None:
                return
            try:
                await self.on_reconnected()
            except Exception:
                log.exception("reconnect callback failed")

        self._nc = await nats.connect(self._url, reconnected_cb=_on_reconnected)
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

    async def publish_sensor(self, reading: SensorReading) -> None:
        """Telemetry, on core pub/sub — deliberately not JetStream.

        History belongs in VictoriaMetrics. Buffering readings on the spine
        would mean a consumer coming back online receives a burst of stale
        measurements, and a control loop acting on a five-minute-old
        temperature is worse than one acting on none.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")
        await self._nc.publish(
            subjects.sensor(reading.sensor_type, reading.sensor_id),
            reading.model_dump_json().encode(),
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

    async def publish_sensor_registration(self, registration: SensorRegistration) -> None:
        """Announce a sensor on the registry subject.

        Same subject family as actuator registration, so one consumer sees every
        device the hub has. hardware-io announces and forgets: it never learns
        whether anything stored the result, which is what keeps it free of a
        database dependency.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")
        await self._nc.publish(
            subjects.registry(registration.sensor_id),
            registration.model_dump_json().encode(),
        )

    async def read_assignments(self) -> list[DeviceAssignment]:
        """Every current assignment, read once at startup.

        A drain of the retained stream rather than a subscription: hardware-io
        builds its drivers once and a binding change is a restart, which keeps
        driver construction off the hot path and out of the supervisor loop.

        An unreadable message is skipped and logged rather than fatal. One
        malformed assignment must not stop a hub bringing up the devices that
        are fine — the opposite of the device file, where a bad entry stopped
        everything, because there the file was the whole topology and here it is
        one device among several.
        """
        try:
            sub = await self.js.pull_subscribe(
                subjects.ALL_ASSIGNMENTS,
                durable=None,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.LAST_PER_SUBJECT),
            )
        except NotFoundError:
            log.warning("assignment stream not provisioned; no devices to build")
            return []

        found: list[DeviceAssignment] = []
        while True:
            try:
                msgs = await sub.fetch(32, timeout=1.0)
            except (TimeoutError, nats.errors.TimeoutError):
                break
            for msg in msgs:
                try:
                    found.append(DeviceAssignment.model_validate_json(msg.data))
                except ValidationError:
                    log.warning(
                        "assignment did not validate; skipped",
                        extra={"subject": msg.subject},
                    )
                await msg.ack()
        with contextlib.suppress(Exception):
            await sub.unsubscribe()
        return found

    async def watch_assignments(self, on_message: Callable[[bytes], None]) -> None:
        """Core subscribe to live assignment traffic, payload and all.

        The raw payload is handed on rather than swallowed (changed
        2026-08-15). This was payload-blind on the reasoning that any message
        here means the registry moved and the response is the same regardless
        — true of a *change*, but the API republishes every adopted assignment
        on every lifespan start, so most messages on this subject say nothing
        new. Deciding which is which needs the payload; what to do about a real
        change is unchanged, and still a restart.

        Retained JetStream state is NOT redelivered on a core subscription, so
        startup's own read never triggers this.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")

        async def _cb(msg: Msg) -> None:
            on_message(msg.data)

        await self._nc.subscribe(subjects.ALL_ASSIGNMENTS, cb=_cb)
        log.info("watching assignments", extra={"subject": subjects.ALL_ASSIGNMENTS})

    async def subscribe_heartbeats(self, component: str, on_beat: Callable[[], None]) -> None:
        """Core subscription to one component's liveness beacon.

        Core pub/sub on purpose, like the publish side: a replayed heartbeat
        would make a dead controller look alive. The callback is synchronous
        and cheap (InterlockSupervisor.heartbeat stamps a monotonic time) —
        nothing here may block the NATS client's task. Malformed payloads are
        dropped with a warning, same contract as watch_assignments. Parsing
        and handling are guarded separately (mirrors control-engine
        publisher.py's subscribe_sensors/subscribe_assignments): a raising
        on_beat must not escape this callback into nats.py's default error
        path, which logs via stdlib and is invisible to our structured-log
        grep, and must not kill the subscription.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")

        async def _cb(msg: Msg) -> None:
            try:
                Heartbeat.model_validate_json(msg.data)
            except ValidationError:
                log.warning("dropping an undecodable heartbeat", extra={"subject": msg.subject})
                return
            try:
                on_beat()
            except Exception:  # broad by design - see docstring
                log.exception("heartbeat handling failed", extra={"subject": msg.subject})

        await self._nc.subscribe(subjects.heartbeat(component), cb=_cb)
        log.info("watching heartbeats", extra={"component": component})

    async def publish_capabilities(self, announcement: CapabilityAnnouncement) -> None:
        """Announce what one hardware source can offer.

        The whole channel list per source, not one message per channel: a source
        that loses a channel says so by republishing a shorter list, where
        per-channel messages could only ever add.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")
        await self._nc.publish(
            subjects.capability(announcement.hardware_source),
            announcement.model_dump_json().encode(),
        )

    async def publish_chip_state(self, state: ChipState) -> None:
        """Announce how one hardware source instance is configured.

        Retained last-value per (source, instance): re-initialisation after a
        bus fault republishes, and a consumer that starts late reads the
        current configuration instead of waiting for the next restart.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")
        await self._nc.publish(
            subjects.chip(state.hardware_source, state.instance),
            state.model_dump_json().encode(),
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


#: JetStream's error code for "filtered consumer not unique on workqueue
#: stream". Distinct from a vanished consumer, and it needs a distinct message:
#: no amount of retrying re-creates a consumer whose filter subject somebody
#: else is holding.
_FILTER_NOT_UNIQUE = 10100


class ConsumerLostError(RuntimeError):
    """The command consumer could not be recovered within its retry budget.

    Raised rather than calling ``os._exit`` so the service's own shutdown runs:
    the point of giving up is to reach the restart path, and that path begins
    with driving every actuator to its safe state.
    """

    def __init__(self, durable: str) -> None:
        super().__init__(f"command consumer {durable!r} could not be re-established")
        self.durable = durable


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
        self._heal_attempts = 0
        #: Set while a heal is in flight and has not yet succeeded. It is what
        #: lets ``drain_once`` tell "lost mid-flight" from "never subscribed".
        self._heal_cause: Exception | None = None

    #: How many consecutive re-subscribe attempts before giving up and letting
    #: the process die.
    #:
    #: Bounded on purpose. Retrying forever means a hub whose stream has been
    #: deleted sits "running" and consumes nothing — the failure this codebase
    #: keeps meeting, wearing a different hat. Exiting hands the problem to the
    #: restart path, which is the one recovery mechanism that has been drilled:
    #: the supervisor asserts every actuator into its safe state on startup.
    MAX_HEAL_ATTEMPTS = 5
    #: Seconds between attempts; the last is roughly 8s, so the whole budget is
    #: about 15 seconds before the process gives up.
    HEAL_BACKOFF_S = 0.5

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
            if self._heal_cause is None:
                # Never subscribed at all. That is a caller mistake, not a
                # broker event, and healing would paper over it.
                raise RuntimeError("not subscribed")
            # A heal ran and did not stick. Keep healing so the attempt budget
            # advances to ConsumerLostError and the drilled restart path runs.
            # Falling through to the bare RuntimeError above is how this
            # service died on the hub with its budget still untouched.
            await self._heal(self._heal_cause)
            return []
        try:
            msgs = await self._sub.fetch(batch, timeout=timeout)
        except (TimeoutError, nats.errors.TimeoutError):
            return []
        except (NotFoundError, ServiceUnavailableError) as exc:
            # The consumer is gone from under us — deleted by an operator, or
            # lost with the stream. Previously this escaped and killed the
            # process, which is a real way to lose command handling for the
            # length of a restart because somebody ran a cleanup script.
            await self._heal(exc)
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

            try:
                outcome = await self._supervisor.apply(command)
            except Exception:
                # A driver fault (chip off the bus, transient I2C error) is a
                # hardware event, not a reason to unwind the process — that
                # turned one off-bus chip into a crash-loop, because the
                # un-acked workqueue message redelivered into every restart.
                # Nak with a delay instead: a transient fault succeeds on one
                # of the redeliveries. A persistent one does not converge on
                # the command's own TTL — max_deliver (3, ~1s nak cadence)
                # exhausts in a few seconds, long before a 30s TTL would ever
                # matter, and JetStream then stops redelivering the message
                # at all. It sits unacked on BR_CMD until the stream's own
                # max_age (1 hour, DiscardPolicy.OLD) reaps it — that is the
                # actual convergence to silence, not an expiry check. The
                # log.critical call below fires once per delivery attempt, so
                # up to three of them are the standing record of that
                # abandonment; no separate audit event is emitted for
                # delivery-exhaustion itself, because a pull consumer exposes
                # no per-message "this was the last attempt" hook to hang one
                # off of, and at this scale the log record is enough.
                log.critical(
                    "driver failed applying a command; message nak'd for redelivery",
                    extra={"actuator_id": command.actuator_id},
                    exc_info=True,
                )
                await msg.nak(delay=1.0)
                continue
            outcomes.append(outcome)

            if outcome == "applied":
                await msg.ack()
                await self._publish_applied_state(command)
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

    async def _heal(self, cause: Exception) -> None:
        """Re-provision and re-subscribe, or die trying.

        CRITICAL, not warning: losing the command consumer means commands are
        being published and nothing is applying them, which is indistinguishable
        from a healthy hub right up until an actuator does not move.

        Re-provisioning before re-subscribing is deliberate. If the stream went
        with the consumer, subscribing alone would fail the same way forever;
        ``provision`` is idempotent, so paying for it costs nothing in the
        common case where only the consumer vanished.
        """
        self._heal_attempts += 1
        self._sub = None
        self._heal_cause = cause
        log.critical(
            "command consumer lost; commands are not being applied",
            extra={
                "durable": self._durable,
                "attempt": self._heal_attempts,
                "limit": self.MAX_HEAL_ATTEMPTS,
                "cause": type(cause).__name__,
            },
        )

        if self._heal_attempts > self.MAX_HEAL_ATTEMPTS:
            log.critical(
                "could not recover the command consumer; exiting for restart",
                extra={"durable": self._durable, "attempts": self._heal_attempts},
            )
            # The restart path is the drilled one: on startup the supervisor
            # asserts every actuator into its declared safe state before doing
            # anything else. Staying up without a consumer has no such
            # guarantee — it just looks healthy.
            raise ConsumerLostError(self._durable)

        await asyncio.sleep(self.HEAL_BACKOFF_S * (2 ** (self._heal_attempts - 1)))
        try:
            await self._spine.provision()
            await self.subscribe()
        except BadRequestError as exc:
            if exc.err_code == _FILTER_NOT_UNIQUE:
                # Not "the consumer vanished" — "somebody else holds the filter".
                # A workqueue stream permits exactly one consumer per filter
                # subject, so re-creating ours cannot succeed until the other
                # one goes. Usually a leaked test durable. Named explicitly
                # because the operator action is different: find it and delete
                # it, rather than wait for a retry that cannot work.
                log.critical(
                    "another consumer already holds this filter subject on the "
                    "workqueue; hardware-io cannot bind until it is deleted",
                    extra={
                        "durable": self._durable,
                        "attempt": self._heal_attempts,
                        "limit": self.MAX_HEAL_ATTEMPTS,
                    },
                )
            else:
                log.exception("re-subscribe failed", extra={"durable": self._durable})
            return
        except Exception:
            log.exception("re-subscribe failed", extra={"durable": self._durable})
            return

        log.warning(
            "command consumer recovered",
            extra={"durable": self._durable, "attempts": self._heal_attempts},
        )
        self._heal_attempts = 0
        self._heal_cause = None

    async def _audit(self, event_type: str, detail: dict[str, object]) -> None:
        """Best-effort: an audit publish failure is logged, never raised.

        The ack/term deciding the message's fate must happen regardless — a
        JetStream hiccup on the audit stream was escaping drain_once and
        exiting the process mid-refusal (2026-08-23 finding 5), while the
        state publishes next door were already wrapped."""
        try:
            # detail spread last: no publisher stamps its own "actor" in
            # detail today, but if one ever does, its explicit value should
            # win over this constant rather than being silently overwritten.
            await self._spine.publish_audit(
                "command", {"event": event_type, "actor": "hardware-io", **detail}
            )
        except Exception:
            log.warning(
                "audit publish failed; event dropped",
                extra={"event": event_type},
                exc_info=True,
            )

    async def _publish_applied_state(self, command: ActuatorCommand) -> None:
        """Tell the wire what an applied command actually did.

        This is the layer that has the spine: the supervisor (safety.py) must
        keep enforcing interlocks with no broker at all, so it stays
        publish-blind by design and this call site is the trade-off — not a
        callback threaded through it. A publish failure must never make a
        successfully applied command look like it failed, so it is logged and
        swallowed exactly like a failed sensor reading (app.py's
        ``_publish_reading``).

        The published level is what the driver reports via ``read_back()`` —
        the post-hardware truth, not the commanded one. A PWM driver's own
        snap_duty rule (dimming.py) can turn a 5% command into a dark pin;
        publishing ``command.level`` unconditionally would have shown 5% on
        the truth line and archived 5% in VictoriaMetrics while the channel
        sat at 0%.

        Falls back to ``driver.effective_level(command.level)`` — the same
        pure snap prediction safety.py's max-runtime clock now keys on
        (2026-08-29) — when the driver cannot report a measured value at all.
        This is not a corner case: the PCA9685 (pca9685.py:528) *always*
        returns ``None`` from ``read_back()``, deliberately, because its
        registers only echo what was written and prove nothing about the
        LED driver. Before this fallback existed, that meant the PCA9685 leg
        published the raw commanded level unconditionally — so a 5% command
        on an adopted PCA9685 channel would have shown 5% on the wire and in
        VictoriaMetrics while the supervisor (correctly, post the same-day
        fix) already considered the channel dark: the exact divergence this
        method's docstring describes, reached through the "driver cannot
        report" branch rather than the read_back-disagrees one.
        ``command.level`` remains the last resort, on a ``read_back()``
        failure — a raised exception, not a driver's honest ``None``.
        """
        level: ActuatorLevel = command.level
        try:
            driver = self._supervisor.driver_of(command.actuator_id)
            read = await driver.read_back()
            level = read if read is not None else driver.effective_level(command.level)
        except Exception:
            log.warning(
                "could not determine effective level; publishing the commanded level instead",
                extra={"actuator_id": command.actuator_id},
                exc_info=True,
            )

        try:
            await asyncio.wait_for(
                self._spine.publish_state(
                    ActuatorState(
                        message_id=uuid4(),
                        emitted_at=utcnow(),
                        source=SOURCE,
                        actuator_id=command.actuator_id,
                        level=level,
                        reason="commanded",
                        since=utcnow(),
                        latched=False,
                    )
                ),
                timeout=_PUBLISH_TIMEOUT_S,
            )
        except Exception:
            log.warning(
                "failed to publish actuator state",
                extra={"actuator_id": command.actuator_id},
                exc_info=True,
            )


def utcnow() -> datetime:
    return datetime.now(UTC)

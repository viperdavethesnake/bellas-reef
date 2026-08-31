# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Wire payload models.

Every message on the spine is one of these. All models are frozen and reject
unknown fields: an unrecognised key is a contract violation, not something to
tolerate quietly, because the sender and receiver may be different hardware
generations.

Timestamps are timezone-aware without exception. A naive datetime on a
controller that schedules dosing is a latent incident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

__all__ = [
    "LIGHT_HEARTBEAT_TIMEOUT_S",
    "LIGHT_MAX_RUNTIME_S",
    "SCHEMA_VERSION",
    "ActuatorClass",
    "ActuatorCommand",
    "ActuatorLevel",
    "ActuatorRegistration",
    "ActuatorRole",
    "ActuatorState",
    "AlertBound",
    "AlertClass",
    "AlertState",
    "BinaryLevel",
    "CapabilityAnnouncement",
    "CapabilityChannel",
    "CapabilitySource",
    "ChipState",
    "ControlAuthority",
    "DeviceAssignment",
    "DeviceId",
    "Heartbeat",
    "HostStatus",
    "PwmLevel",
    "SensorAlert",
    "SensorReading",
    "SensorRegistration",
    "SensorSilence",
    "StateReason",
    "Transport",
]

#: Wire schema version. Bumping this is a MAJOR contract change — see the semver
#: policy in docs/contracts/nats-subjects.md.
#:
#: v2 added the required ``role`` on ActuatorRegistration.
#:
#: Known bluntness, accepted deliberately: this lives on the shared envelope,
#: so bumping it for a change that touches only one message type also signals
#: "changed" for every other. Per-message-type versioning would be finer but
#: is not worth the machinery at two consumers.
SCHEMA_VERSION: Final[Literal[2]] = 2

#: Device identifiers double as NATS subject tokens, so they carry the same
#: restrictions. See :mod:`bellasreef_contracts.subjects`.
DeviceId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]

ActuatorClass = Literal["binary", "pwm"]

#: What an actuator *is for*, as opposed to how it is driven.
#:
#: ``actuator_class`` is the electrical contract (on/off vs proportional);
#: ``role`` is the husbandry one. A client renders by role — a light gets a
#: day curve, a doser gets millilitres — while the wire stays class-based.
#:
#: Required, not optional. An actuator with an unspecified role forces every
#: client to guess from its id, and removing that guess is the entire point.
#: Values trace to the PRD control modules: light R7, heater R5, pump R6,
#: doser R9, outlet R8. Only ``light`` is implemented; the rest are reserved
#: so adding them later is not another breaking change.
ActuatorRole = Literal["light", "heater", "pump", "doser", "outlet"]

#: The safety contract every dimmable light registers with, stated once.
#:
#: Both PWM drivers use these, and the API writes them onto the device row when
#: a light is bound — the row's CHECK requires an actuator to declare its
#: authority, and a device bound through the API must satisfy the same
#: constraint as one registered by hardware-io. Two copies of these numbers is
#: two things that can drift, so there is one.
#:
#: 18 hours is a runaway bound, not a photoperiod: a reef light legitimately
#: runs 10-12 hours, and a cap near that trips on an ordinary Tuesday and
#: teaches the operator to ignore it.
LIGHT_MAX_RUNTIME_S: Final = 18 * 3600.0

#: Lose the control engine for half a minute and the channel goes dark — a
#: visible, survivable failure, which is why lighting was the first actuator.
LIGHT_HEARTBEAT_TIMEOUT_S: Final = 30.0

#: Whether we can actually make the device obey. See docs/device-classes.md §2.
#:
#: Orthogonal to ``actuator_class`` (what the device is electrically) and
#: ``role`` (what it does in the tank). Neither of those says anything about
#: whether a command is a guarantee or a hope, and the safety framework depends
#: entirely on the difference.
#:
#: ``authoritative`` — we own it. Synchronous, deterministic, verifiable at the
#: electrical layer. The full R1 safety triple applies and is enforced.
#: ``advisory`` — we send intent. A dropped command is an expected outcome, not
#: an incident. A safe state cannot be declared because it could not be honoured.
#: ``observe_only`` — registered for coordination, never written to. The command
#: path is closed at registration, and a safe state is refused for the same
#: reason as advisory: nothing could ever apply it.
ControlAuthority = Literal["authoritative", "advisory", "observe_only"]

#: How the device is reached. Local buses are deterministic; a network is not.
Transport = Literal["local", "network"]

StateReason = Literal[
    "commanded",
    "safe_state",
    "interlock_latch",
    "manual_override",
    "startup",
]


class _Frozen(BaseModel):
    """Base config shared by every contract model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _Message(_Frozen):
    """Common envelope on every message published to the spine."""

    schema_version: Literal[2] = SCHEMA_VERSION
    message_id: UUID
    emitted_at: AwareDatetime
    source: DeviceId


# --------------------------------------------------------------- levels


class BinaryLevel(_Frozen):
    """An on/off actuator level — relays, outlets, solenoids."""

    kind: Literal["binary"] = "binary"
    on: bool


class PwmLevel(_Frozen):
    """A proportional level, normalised 0.0–1.0 — LED channels, variable pumps.

    Normalised deliberately: the contract must not leak a driver's native
    resolution (PCA9685 is 12-bit) to the control engine.
    """

    kind: Literal["pwm"] = "pwm"
    duty: float = Field(ge=0.0, le=1.0)


ActuatorLevel = Annotated[BinaryLevel | PwmLevel, Field(discriminator="kind")]


# --------------------------------------------------------------- messages


class SensorReading(_Message):
    """One sample from one sensor.

    Published on ``bellasreef.sensor.<type>.<id>`` as core pub/sub. History
    lives in VictoriaMetrics, so these are deliberately not persisted on the
    spine.
    """

    sensor_id: DeviceId
    sensor_type: DeviceId
    value: float | None
    unit: str = Field(min_length=1, max_length=16)
    quality: Literal["ok", "stale", "fault"] = "ok"
    calibration_id: UUID | None = None

    @model_validator(mode="after")
    def _value_required_when_ok(self) -> Self:
        if self.quality == "ok" and self.value is None:
            raise ValueError("quality='ok' requires a value; use 'fault' to report a failed read")
        return self


#: What a hardware source can offer, as opposed to what anyone has decided to
#: do with it.
CapabilitySource = Literal["pi-pwm", "pca9685", "w1-bus"]


class CapabilityChannel(_Frozen):
    """One offerable thing: a PWM channel, a probe on the bus."""

    #: Stable within its source, and what a binding names. The channel number
    #: for PWM, the ROM code for a 1-Wire probe.
    channel: str = Field(min_length=1, max_length=64)

    #: Anything a client should render — the GPIO a channel reaches, the I²C
    #: address, the bus master. Free-form because it differs per source and no
    #: consumer should switch on it.
    detail: dict[str, str | int | float | bool] = Field(default_factory=dict)


class CapabilityAnnouncement(_Message):
    """What one hardware source can offer, published on startup.

    Tier one of the registry. Deliberately not a device: a capability is a fact
    about the hardware — this hub has four PWM channels — true whether or not
    anybody has decided what they are for. A device is the operator's decision
    that *that* channel is the blue LED.

    Carries the source's **whole** channel list rather than one message per
    channel, so a source that loses a channel can say so by republishing a
    shorter list. One message per channel could only ever add.

    Published on ``bellasreef.capability.<source>`` and retained last-value per
    subject, so a consumer that starts late still learns the topology without
    waiting for a hardware-io restart.
    """

    #: Which hardware source this describes. Deliberately NOT ``source``, which
    #: the message envelope already uses for *who published this* — overriding
    #: it would silently discard the publisher's identity on every
    #: announcement.
    hardware_source: CapabilitySource
    channels: list[CapabilityChannel]


class ChipState(_Message):
    """What one hardware source is configured as, right now.

    Published on ``bellasreef.chip.<source>.<instance>`` and retained
    last-value per subject, like a capability announcement — a consumer that
    starts late still learns how the chip is set up. Per hardware source
    instance, not per channel: frequency, polarity, output mode and
    "initialised" are properties of the chip (spec 2026-08-19).
    """

    hardware_source: CapabilitySource
    instance: str = Field(min_length=1, max_length=64)
    initialised: bool
    initialised_at: AwareDatetime | None = None
    #: Facts a client renders as a table. Free-form for the same reason
    #: CapabilityChannel.detail is: they differ per source and no consumer
    #: should switch on them. Keys are stable strings; values scalars.
    facts: dict[str, str | int | float | bool] = Field(default_factory=dict)


class HostStatus(_Message):
    """The hub machine's own vitals, right now.

    Published on ``bellasreef.host.status`` every 30 s and retained
    last-value, like a chip state — a consumer that starts late still gets
    the most recent snapshot. Snapshot only, deliberately: this is a status
    page's data, not history (contracts 4.3.0 spec,
    docs/superpowers/specs/2026-08-31-hub-status-design.md).

    ``/proc`` loadavg/meminfo/uptime are system-wide even inside a
    container, and ``/sys`` is the host's — measured in
    bellasreef-hardware-io-1 on coco 2026-08-31, which is what makes
    hardware-io able to publish this with no new mounts or privileges.

    No hostname field: in-container the hostname is the container's, and
    clients already get the hub's name from ``/api/v1/info``. A phase-2
    spoke identifies itself in the envelope's ``source``.
    """

    load_1m: float = Field(ge=0)
    load_5m: float = Field(ge=0)
    load_15m: float = Field(ge=0)
    cpu_count: int = Field(ge=1)
    mem_total_kb: int = Field(ge=0)
    mem_available_kb: int = Field(ge=0)
    #: SoC temperature in °C. None means unreadable on this host (no thermal
    #: zone) — a real state, never to be papered over with a fabricated 0.
    temp_c: float | None = None
    uptime_s: float = Field(ge=0)


class DeviceAssignment(_Message):
    """An operator's decision about one device, for hardware-io to act on.

    Tier two of the registry, on the wire. The API publishes one of these
    whenever a device is bound or unbound; hardware-io reads them at startup and
    builds exactly the drivers they describe.

    Deliberately not fetched over HTTP. hardware-io holds no credential and the
    API's only unauthenticated endpoints are ``/info`` and ``/healthz`` by
    design — giving hardware-io a token so it could read its own configuration
    would be a new secret to manage for no gain. It is already on the spine.

    Published on ``bellasreef.assignment.<device_id>`` and retained last-value
    per subject, so a hardware-io that restarts alone still learns every
    assignment rather than waiting for someone to re-save each device.

    ``adopted=False`` is the tombstone: an unbind republishes with it, which
    both removes the driver and leaves a record that the channel is free. A
    deleted subject would simply vanish and a consumer that was offline would
    never learn the device went away.
    """

    device_id: DeviceId
    adopted: bool
    role: ActuatorRole | None = None
    driver_type: str | None = None
    #: Driver-specific, validated at the API boundary before it is published:
    #: ``{"channel": "0"}`` for pi-pwm, ``{"rom": "28-..."}`` for ds18b20.
    binding: dict[str, str] | None = None

    @model_validator(mode="after")
    def _adopted_means_bound(self) -> Self:
        """An adopted device with nowhere to be is not an assignment.

        Mirrors the database CHECK. Both layers, because this is what
        hardware-io builds from and a half-filled assignment would produce a
        driver pointed at nothing.
        """
        if self.adopted and (self.driver_type is None or self.binding is None):
            raise ValueError("an adopted assignment requires driver_type and binding")
        return self


AlertState = Literal["breach", "clear"]

#: Which side of the band was crossed. Carried explicitly rather than inferred
#: from ``value`` vs ``threshold``, because on a *clear* the value is by
#: definition back inside the band and the comparison no longer identifies it.
AlertBound = Literal["min", "max"]


#: Which kind of thing went wrong. Lives on the episode and in the database
#: rather than as a field on :class:`SensorAlert`, because the two classes carry
#: genuinely different payloads: a threshold breach has a reading and a bound, a
#: silence has neither, and that is the whole point of it.
AlertClass = Literal["threshold", "silence"]


class SensorSilence(_Message):
    """A probe stopped reporting, or started again.

    Published on ``bellasreef.silence.<device_id>``. Deliberately NOT on
    ``bellasreef.alert.<device_id>``: ``ALL_ALERTS`` is a ``>`` wildcard, so a
    payload of a different shape arriving there would be handed to consumers
    that are contractually required to reject it loudly.

    A separate message type rather than a widened :class:`SensorAlert`. Adding a
    field to an existing message is a MAJOR change under the versioning table in
    ``docs/contracts/nats-subjects.md``, and making ``value``/``threshold``/
    ``bound`` optional would weaken them in every generated client for the sake
    of a class that never populates them. A new type on a new subject is MINOR
    and leaves both models strict.

    Silence is a different emergency from a breach. A probe reading 40 °C tells
    you the heater is stuck; a probe saying nothing tells you that you do not
    know what the tank is doing, which is worse and reads on a dashboard as
    calm. It also has to suppress threshold evaluation while it lasts: the last
    number a dead probe published is not evidence about now.
    """

    device_id: DeviceId
    sensor_type: DeviceId
    state: AlertState

    #: How long the probe had been quiet when this was emitted.
    silent_for_s: float = Field(ge=0)

    #: The deadline that was applied, carried so a client can say *why* 45s of
    #: quiet counted. Derived from the probe's declared cadence, so it differs
    #: per device and is not something a client could recompute.
    silence_threshold_s: float = Field(gt=0)

    #: ``None`` for a probe that has not reported since the engine started.
    #: There is genuinely nothing to point at in that case, and inventing a
    #: timestamp would make "never seen" indistinguishable from "seen at boot".
    last_reading_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _a_raise_must_satisfy_its_own_deadline(self) -> Self:
        """A breach that has not actually exceeded its threshold is a bug.

        Same guard as SensorAlert's inverted-comparison check, for the same
        reason: an evaluator comparing against the wrong side of the deadline
        produces alerts that look entirely plausible downstream.

        Only on ``breach``. A clear reports how long the silence lasted, which
        is history rather than a trigger, and clamping it would lose the
        duration on any silence that ended quickly.
        """
        if self.state == "breach" and self.silent_for_s < self.silence_threshold_s:
            raise ValueError(
                f"silence breach requires silent_for_s >= silence_threshold_s "
                f"({self.silent_for_s} < {self.silence_threshold_s})"
            )
        return self


class SensorAlert(_Message):
    """A sensor reading crossed a configured threshold, or came back inside it.

    Published on ``bellasreef.alert.<device_id>``. One alert describes one bound
    on one sensor: a probe with both a min and a max can be in breach of either
    independently, and collapsing them would make "the tank is too cold" and
    "the tank is too hot" indistinguishable to a client.

    ``clear_margin`` travels with the event because the clear threshold is not
    derivable from the breach one without it, and a client that wants to explain
    *why* a reading of 25.4 has not yet cleared a max of 25.0 needs the margin to
    say so.
    """

    device_id: DeviceId
    sensor_type: DeviceId
    state: AlertState
    bound: AlertBound
    #: The reading that triggered this transition. Never ``None``: a faulted read
    #: does not evaluate thresholds at all, so an alert always has a number
    #: behind it.
    value: float
    threshold: float
    clear_margin: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _breach_is_actually_outside_the_band(self) -> Self:
        """A breach must be on the far side of its own threshold.

        This catches an evaluator that has its comparison inverted — the kind of
        bug that reports "too cold" while the heater is cooking the tank, and
        which no amount of downstream rendering can detect.
        """
        if self.state != "breach":
            return self
        if self.bound == "min" and self.value >= self.threshold:
            raise ValueError(
                f"min breach requires value < threshold ({self.value} >= {self.threshold})"
            )
        if self.bound == "max" and self.value <= self.threshold:
            raise ValueError(
                f"max breach requires value > threshold ({self.value} <= {self.threshold})"
            )
        return self


class ActuatorCommand(_Message):
    """A command addressed to exactly one actuator.

    Two fields are mandatory and carry the safety weight:

    ``idempotency_key``
        Published as the ``Nats-Msg-Id`` header so JetStream de-duplicates
        redelivery within the stream's duplicate window.

    ``expires_at``
        The consumer re-checks this against its own clock immediately before
        actuating. Broker-side TTL is defence in depth; **this field is
        authoritative**. A command that arrives late is dropped and audited,
        never executed.
    """

    actuator_id: DeviceId
    actuator_class: ActuatorClass
    level: ActuatorLevel
    idempotency_key: UUID
    expires_at: AwareDatetime
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.level.kind != self.actuator_class:
            raise ValueError(
                f"level.kind={self.level.kind!r} does not match "
                f"actuator_class={self.actuator_class!r}"
            )
        if self.expires_at <= self.emitted_at:
            raise ValueError("expires_at must be strictly after emitted_at")
        return self

    def is_expired(self, now: datetime) -> bool:
        """True if this command must not be executed.

        ``now`` must be timezone-aware; comparing against a naive clock is the
        bug this guards against.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now >= self.expires_at


class ActuatorState(_Message):
    """Last-known state of one actuator, published after every transition."""

    actuator_id: DeviceId
    level: ActuatorLevel
    reason: StateReason
    since: AwareDatetime
    latched: bool = False

    #: Whether the device acknowledged the command that produced this level.
    #:
    #: ``None`` for an authoritative device, where the question is meaningless:
    #: the level is verifiable at the electrical layer, so there is nothing to
    #: acknowledge. Populated by a bridge that speaks to a device over a network
    #: it does not control, which is the only case where "we asked" and "it
    #: happened" are different statements.
    #:
    #: Required by docs/device-classes.md §4 as a label on advisory telemetry
    #: series. Added optional rather than required because every producer that
    #: exists today is authoritative — and because a value here on an
    #: authoritative series would be noise that still forks the series.
    command_acked: bool | None = None

    #: Seconds since the last successful exchange with the device.
    #:
    #: The number behind "when did we stop knowing". ``None`` for authoritative
    #: devices for the same reason as above — a local bus exchange either
    #: happened or the write raised.
    last_exchange_age_s: float | None = Field(default=None, ge=0.0)


class Heartbeat(_Message):
    """Liveness beacon.

    Never persisted or replayed — a stale heartbeat delivered from a stream
    would make a dead control-engine look alive, defeating the mechanism.
    """

    component: DeviceId
    sequence: int = Field(ge=0)
    interval_s: float = Field(gt=0.0)


class SensorRegistration(_Message):
    """Declaration that a sensor exists, and at what cadence it reports.

    The counterpart to :class:`ActuatorRegistration`, and deliberately thinner:
    a sensor has no safe state to declare because it cannot do anything unsafe.
    What it must declare is its cadence, because that is what makes "this
    reading is stale" a decidable question rather than a guess.

    Published on ``bellasreef.registry.<sensor_id>``. hardware-io announces; it
    never writes to Postgres. The database is downstream of the spine, which is
    what lets a phase-2 ESP32 spoke register itself without knowing a database
    exists.
    """

    sensor_id: DeviceId
    sensor_type: DeviceId
    driver_id: DeviceId
    #: How the probe is reached. Required, no default, for the same reason as on
    #: an actuator: an unstated value silently reads as the trustworthy one.
    #:
    #: A sensor has no *control* authority — it controls nothing — but it does
    #: have provenance, and that is the axis that matters for a reading. A
    #: 1-Wire probe on the board is measured; a value relayed from a vendor's
    #: cloud is reported. Charted without a distinguishing label those two lines
    #: are identical, which is the same failure §4 describes for actuators.
    transport: Transport
    unit: str = Field(min_length=1, max_length=16)
    #: Nominal seconds between reads. A consumer deciding whether a reading has
    #: gone stale needs the cadence the driver actually intends, not a constant
    #: someone picked in a client.
    poll_interval_s: float = Field(gt=0.0)
    description: str | None = Field(default=None, max_length=512)


class ActuatorRegistration(_Message):
    """Declaration that an actuator exists and how it must fail.

    ``safe_state``, ``max_runtime_s`` and ``heartbeat_timeout_s`` have no
    defaults on purpose. An actuator whose failure behaviour is unspecified must
    not be registerable at all — that is the whole safety framework in one
    model.
    """

    actuator_id: DeviceId
    actuator_class: ActuatorClass
    role: ActuatorRole
    driver_id: DeviceId

    #: Required, no default. A default here would mean an unstated authority
    #: silently reads as the strongest one — which is the exact failure this
    #: field exists to prevent.
    control_authority: ControlAuthority
    failsafe_capable: bool
    transport: Transport

    #: The R1 safety triple. Optional on the *model* and mandatory in the
    #: *validator* for authoritative devices, because an advisory device must be
    #: unable to declare a safe state at all — see `_authority_governs_safety`.
    safe_state: ActuatorLevel | None = None
    max_runtime_s: float | None = Field(default=None, gt=0.0)
    heartbeat_timeout_s: float | None = Field(default=None, gt=0.0)

    description: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _authority_governs_safety(self) -> Self:
        """Enforce docs/device-classes.md §2.1 and §2.2.

        The asymmetry is the point. An authoritative registration without the
        triple is rejected because it claims a guarantee it has not specified.
        An advisory registration *with* a safe state is rejected because it
        specifies a guarantee it cannot keep — and accepting-and-ignoring it
        would leave a value in the schema that reads exactly like an enforced
        one.
        """
        if self.control_authority == "authoritative":
            if not self.failsafe_capable:
                raise ValueError("authoritative devices must be failsafe_capable")
            if self.transport != "local":
                raise ValueError("authoritative devices must be transport='local'")
            missing = [
                name
                for name, value in (
                    ("safe_state", self.safe_state),
                    ("max_runtime_s", self.max_runtime_s),
                    ("heartbeat_timeout_s", self.heartbeat_timeout_s),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    "authoritative devices must declare the full safety triple; "
                    f"missing: {', '.join(missing)}"
                )
        elif self.safe_state is not None:
            # advisory (§2.2) and observe_only (§2.3) alike. For observe_only the
            # argument is stronger, not weaker: no command is ever emitted, so
            # there is no path by which a safe state could be applied at all.
            raise ValueError(
                f"{self.control_authority} devices must not declare a safe_state: it "
                "could not be enforced, and a value here is indistinguishable from "
                "one that is"
            )
        return self

    @model_validator(mode="after")
    def _safe_state_matches_class(self) -> Self:
        # Only meaningful when a safe state exists at all — advisory and
        # observe_only devices carry none.
        if self.safe_state is None:
            return self
        if self.safe_state.kind != self.actuator_class:
            raise ValueError(
                f"safe_state.kind={self.safe_state.kind!r} does not match "
                f"actuator_class={self.actuator_class!r}"
            )
        return self

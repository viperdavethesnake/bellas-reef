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
    "SCHEMA_VERSION",
    "ActuatorClass",
    "ActuatorCommand",
    "ActuatorLevel",
    "ActuatorRegistration",
    "ActuatorState",
    "BinaryLevel",
    "DeviceId",
    "Heartbeat",
    "PwmLevel",
    "SensorReading",
    "StateReason",
]

#: Wire schema version. Bumping this is a MAJOR contract change — see the semver
#: policy in docs/contracts/nats-subjects.md.
SCHEMA_VERSION: Final[Literal[1]] = 1

#: Device identifiers double as NATS subject tokens, so they carry the same
#: restrictions. See :mod:`bellasreef_contracts.subjects`.
DeviceId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]

ActuatorClass = Literal["binary", "pwm"]

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

    schema_version: Literal[1] = SCHEMA_VERSION
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


class Heartbeat(_Message):
    """Liveness beacon.

    Never persisted or replayed — a stale heartbeat delivered from a stream
    would make a dead control-engine look alive, defeating the mechanism.
    """

    component: DeviceId
    sequence: int = Field(ge=0)
    interval_s: float = Field(gt=0.0)


class ActuatorRegistration(_Message):
    """Declaration that an actuator exists and how it must fail.

    ``safe_state``, ``max_runtime_s`` and ``heartbeat_timeout_s`` have no
    defaults on purpose. An actuator whose failure behaviour is unspecified must
    not be registerable at all — that is the whole safety framework in one
    model.
    """

    actuator_id: DeviceId
    actuator_class: ActuatorClass
    driver_id: DeviceId

    safe_state: ActuatorLevel
    max_runtime_s: float = Field(gt=0.0)
    heartbeat_timeout_s: float = Field(gt=0.0)

    description: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _safe_state_matches_class(self) -> Self:
        if self.safe_state.kind != self.actuator_class:
            raise ValueError(
                f"safe_state.kind={self.safe_state.kind!r} does not match "
                f"actuator_class={self.actuator_class!r}"
            )
        return self

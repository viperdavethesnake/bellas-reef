# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""PostgreSQL schema v1.

Four tables, no speculation. Two things are worth reading closely:

* ``devices`` carries a CHECK constraint that mirrors ``ActuatorRegistration``.
  The Pydantic model rejects an actuator with no declared failure behaviour at
  the wire; this rejects it at rest. Both layers, deliberately — the safety
  framework should not have a single point of bypass.

* ``audit_log`` is append-only, enforced by a trigger in migration 0001 rather
  than by convention. UPDATE and DELETE raise.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so Alembic autogenerate produces stable, diffable
# constraint names instead of Postgres defaults.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Device(Base):
    """A registered sensor or actuator.

    ``device_id`` is the NATS subject token, not the primary key — the surrogate
    UUID survives a device being renamed.
    """

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = _uuid_pk()
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # 'sensor' | 'actuator'
    driver_id: Mapped[str] = mapped_column(String(64))

    # Sensor-only
    sensor_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    poll_interval_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Actuator-only. Non-null for actuators, enforced by check constraint below.
    actuator_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: What the actuator is for, as opposed to how it is driven. Required for
    #: actuators, meaningless for sensors (sensor_type already carries it).
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    safe_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    max_runtime_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    heartbeat_timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('sensor', 'actuator')", name="kind_valid"),
        CheckConstraint(
            "actuator_class IS NULL OR actuator_class IN ('binary', 'pwm')",
            name="actuator_class_valid",
        ),
        # The safety framework, at rest. An actuator without a declared safe
        # state, runtime cap and heartbeat timeout cannot be stored at all.
        CheckConstraint(
            """
            kind <> 'actuator' OR (
                actuator_class      IS NOT NULL
            AND safe_state          IS NOT NULL
            AND max_runtime_s       IS NOT NULL AND max_runtime_s > 0
            AND heartbeat_timeout_s IS NOT NULL AND heartbeat_timeout_s > 0
            )
            """,
            name="actuator_declares_failure_behaviour",
        ),
        # The IS NOT NULL is load-bearing, not redundant. With poll_interval_s
        # NULL, `poll_interval_s > 0` is NULL, `TRUE AND NULL` is NULL, and a
        # CHECK that evaluates to NULL PASSES in Postgres. Without this, a
        # sensor could be stored with no declared cadence at all.
        CheckConstraint(
            """
            kind <> 'sensor' OR (
                sensor_type     IS NOT NULL
            AND poll_interval_s IS NOT NULL AND poll_interval_s > 0
            )
            """,
            name="sensor_declares_type_and_cadence",
        ),
        # IS NOT NULL first, for the same reason as the constraints above: a
        # CHECK that evaluates to NULL passes in Postgres, so `role IN (...)`
        # alone would let an actuator with no role straight through.
        CheckConstraint(
            """
            kind <> 'actuator' OR (
                role IS NOT NULL
            AND role IN ('light', 'heater', 'pump', 'doser', 'outlet')
            )
            """,
            name="actuator_declares_role",
        ),
    )


class CalibrationRecord(Base):
    """A fitted calibration. Never updated, never deleted — superseded."""

    __tablename__ = "calibration_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    calibration_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), unique=True)
    device_pk: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("devices.id", ondelete="RESTRICT"), index=True
    )

    coefficients: Mapped[list[float]] = mapped_column(JSONB)
    residual: Mapped[float] = mapped_column(Float)
    points: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    unit: Mapped[str] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(64))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint("residual >= 0", name="residual_non_negative"),)


class DosingTransaction(Base):
    """A dose as a transaction: intent -> execution -> confirmation.

    Modelled as a state machine row rather than an event stream because
    reconciliation asks "what is the state of dose X", not "replay all doses".
    Timestamp presence is tied to state by check constraint, so a row cannot
    claim to be confirmed without evidence of confirmation.
    """

    __tablename__ = "dosing_journal"

    id: Mapped[uuid.UUID] = _uuid_pk()
    device_pk: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("devices.id", ondelete="RESTRICT"), index=True
    )

    #: Ties the row to the ActuatorCommand that carried it. Unique, so a
    #: redelivered command cannot dose twice.
    idempotency_key: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), unique=True)

    state: Mapped[str] = mapped_column(String(16), index=True)

    requested_ml: Mapped[float] = mapped_column(Numeric(10, 3))
    delivered_ml: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)

    container_level_ml_before: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    container_level_ml_after: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    intent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('intent', 'executing', 'confirmed', 'failed', 'aborted')",
            name="state_valid",
        ),
        CheckConstraint("requested_ml > 0", name="requested_ml_positive"),
        CheckConstraint(
            "delivered_ml IS NULL OR delivered_ml >= 0", name="delivered_ml_non_negative"
        ),
        CheckConstraint(
            "state <> 'executing' OR executed_at IS NOT NULL",
            name="executing_has_timestamp",
        ),
        CheckConstraint(
            """
            state <> 'confirmed' OR (
                executed_at  IS NOT NULL
            AND confirmed_at IS NOT NULL
            AND delivered_ml IS NOT NULL
            )
            """,
            name="confirmed_has_evidence",
        ),
        CheckConstraint("state <> 'failed' OR error IS NOT NULL", name="failed_has_reason"),
        Index("ix_dosing_journal_device_intent", "device_pk", "intent_at"),
    )


class AuditLog(Base):
    """Append-only record of every command, config change and state transition.

    Immutability is enforced by a trigger created in migration 0001. UPDATE and
    DELETE raise an exception — this is not merely a convention.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: The envelope's message_id, carried end to end from the publisher.
    #: JetStream is at-least-once, so a redelivered audit event arrives twice;
    #: dedup belongs here, at the terminal store, rather than in pretending the
    #: broker is exactly-once.
    message_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), unique=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    category: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "category IN ('command', 'config', 'auth', 'state', 'safety', 'calibration')",
            name="category_valid",
        ),
    )


class PairedClient(Base):
    """A phone, tablet or browser that has paired with the hub.

    Distinct from :class:`Device`, which is hardware. Paired *clients* are
    people's things; *devices* are the tank's.
    """

    __tablename__ = "paired_clients"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128))

    #: SHA-256 of the refresh token, never the token itself. Set to NULL on
    #: revocation — auth.md says revocation deletes the hash, and keeping the
    #: row preserves the audit trail and the client list.
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        # A revoked client must not keep a usable hash, and a live one must
        # have one. Either state is fine; the mismatch is what would be a bug.
        CheckConstraint(
            "(revoked_at IS NULL) <> (refresh_token_hash IS NULL)",
            name="revoked_iff_hash_cleared",
        ),
    )


class SigningKey(Base):
    """The server-side key JWTs are signed with.

    Generated at first boot. Kept in Postgres rather than a file so that a
    restore of the database restores working sessions with it.
    """

    __tablename__ = "signing_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    secret: Mapped[str] = mapped_column(Text)
    algorithm: Mapped[str] = mapped_column(String(16), server_default="HS256")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PairingRequest(Base):
    """A pending pair awaiting approval from an already-paired client.

    The id is what the new client polls. Expiry is stored rather than computed
    so a request cannot be resurrected by moving the clock — which matters on a
    board with no RTC battery.
    """

    __tablename__ = "pairing_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    client_name: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Set when approved: the client row that was created for it.
    client_pk: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("paired_clients.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'approved', 'denied', 'expired')", name="state_valid"
        ),
        # Approved means a client exists and a decision was recorded. Without
        # this, an "approved" row with no client would mint tokens for nobody.
        CheckConstraint(
            "state <> 'approved' OR (client_pk IS NOT NULL AND decided_at IS NOT NULL)",
            name="approved_has_client",
        ),
    )

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
    text,
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
    #: Whether we can actually make the device obey — docs/device-classes.md §2.
    #: Actuator-only; NULL on sensors.
    control_authority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    failsafe_capable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transport: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: What the actuator is for, as opposed to how it is driven. Required for
    #: actuators, meaningless for sensors (sensor_type already carries it).
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    safe_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    max_runtime_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    heartbeat_timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Sensor-only alert thresholds (PRD R12). All nullable: a sensor with no
    # thresholds is simply not evaluated, which is the default state.
    alert_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    alert_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: How far back inside the band a reading must come before a breach clears.
    #: Separate from the threshold so a reading sitting exactly on the boundary
    #: cannot strobe breach/clear/breach on sensor noise.
    alert_clear_margin: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: What the operator calls this device. Never generated, never defaulted to
    #: the id: an empty display name is how a client knows to fall back to the
    #: id rather than showing "ds18b20-28-000000bfe244" and calling it a name.
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('sensor', 'actuator')", name="kind_valid"),
        # A name of spaces is not a name. Blank-but-present would defeat the
        # fallback: clients check for NULL, and "   " is not NULL.
        CheckConstraint(
            "display_name IS NULL OR length(btrim(display_name)) > 0",
            name="display_name_not_blank",
        ),
        CheckConstraint(
            "actuator_class IS NULL OR actuator_class IN ('binary', 'pwm')",
            name="actuator_class_valid",
        ),
        # Every actuator declares its authority, and sensors carry none.
        CheckConstraint(
            """
            kind <> 'actuator' OR (
                actuator_class    IS NOT NULL
            AND control_authority IS NOT NULL
            AND control_authority IN ('authoritative', 'advisory', 'observe_only')
            AND failsafe_capable  IS NOT NULL
            AND transport         IS NOT NULL
            AND transport         IN ('local', 'network')
            )
            """,
            name="actuator_declares_authority",
        ),
        CheckConstraint(
            """
            kind <> 'sensor' OR (
                control_authority IS NULL
            AND failsafe_capable  IS NULL
            AND transport         IS NULL
            )
            """,
            name="sensors_carry_no_authority",
        ),
        # The safety framework, at rest — now scoped to the authority that can
        # actually honour it (device-classes.md §2.1). This replaces the old
        # blanket rule over every actuator. `IS DISTINCT FROM` rather than `<>`
        # because `NULL <> 'authoritative'` is NULL, and a CHECK that evaluates
        # to NULL passes.
        CheckConstraint(
            """
            control_authority IS DISTINCT FROM 'authoritative' OR (
                failsafe_capable    IS TRUE
            AND transport           = 'local'
            AND safe_state          IS NOT NULL
            AND max_runtime_s       IS NOT NULL AND max_runtime_s > 0
            AND heartbeat_timeout_s IS NOT NULL AND heartbeat_timeout_s > 0
            )
            """,
            name="authoritative_declares_failure_behaviour",
        ),
        # §2.2: an advisory device must be unable to declare a safe state, not
        # merely have one ignored. A stored value is indistinguishable from an
        # enforced one to everything downstream.
        CheckConstraint(
            "control_authority IS DISTINCT FROM 'advisory' OR safe_state IS NULL",
            name="advisory_declares_no_safe_state",
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
        # Thresholds are a sensor concept. An actuator carrying them would be
        # configuration nothing reads.
        CheckConstraint(
            """
            kind = 'sensor' OR (
                alert_min          IS NULL
            AND alert_max          IS NULL
            AND alert_clear_margin IS NULL
            )
            """,
            name="thresholds_are_sensor_only",
        ),
        # IS NULL first throughout, for the reason spelled out above: a CHECK
        # that evaluates to NULL passes.
        CheckConstraint(
            "alert_clear_margin IS NULL OR alert_clear_margin > 0",
            name="clear_margin_positive",
        ),
        CheckConstraint(
            "alert_min IS NULL OR alert_max IS NULL OR alert_min < alert_max",
            name="alert_band_ordered",
        ),
        # A threshold without a margin has no defined clear point, so the breach
        # it raises could never end.
        CheckConstraint(
            """
            (alert_min IS NULL AND alert_max IS NULL)
            OR alert_clear_margin IS NOT NULL
            """,
            name="thresholds_require_clear_margin",
        ),
        # The trap this closes: with both bounds set, the clear zone is
        # [min + margin, max - margin]. Make the margin wider than half the band
        # and that interval is empty — every reading is either in breach or
        # still not cleared, and the alert latches forever. Rejected at rest
        # rather than discovered at 3am.
        CheckConstraint(
            """
            alert_min IS NULL OR alert_max IS NULL OR alert_clear_margin IS NULL
            OR (alert_min + alert_clear_margin) < (alert_max - alert_clear_margin)
            """,
            name="clear_zone_is_reachable",
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


class Override(Base):
    """A temporary manual level that outranks the schedule.

    "Off for 30 minutes" for feeding or maintenance. The duration is what the
    operator asked for, so it is counted in elapsed seconds — immune to DST and
    timezone by construction. ``expires_at`` is a wall-clock deadline persisted
    *only* so the override can be re-armed or lapsed across a restart; within a
    run the engine counts on a monotonic clock.

    One active override per target, enforced by a partial unique index rather
    than by whoever writes the next caller remembering.
    """

    __tablename__ = "overrides"

    id: Mapped[uuid.UUID] = _uuid_pk()
    target: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Why it ended. NULL while active.
    release_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "(released_at IS NULL) = (release_reason IS NULL)",
            name="release_reason_iff_released",
        ),
        CheckConstraint(
            "release_reason IS NULL OR release_reason IN "
            "('expired', 'lapsed', 'manual', 'superseded')",
            name="release_reason_valid",
        ),
        Index(
            "uq_overrides_one_active_per_target",
            "target",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class PairingWindow(Base):
    """A deliberately reopened pairing window (auth.md §1, recovery).

    The fire escape. If every client is lost or revoked there is nobody left to
    approve a new one, and the TOFU-ever window is shut by design — so the
    operator SSHes in and runs `bellasreef pair`, which writes a row here.

    A window row rather than clearing client state, deliberately: deleting
    revoked clients would reopen the TOFU-ever window, which is keyed on rows
    having existed precisely so that revoking everything cannot reopen open
    pairing. The recovery path must not undo the thing it is recovering from.
    """

    __tablename__ = "pairing_windows"

    id: Mapped[uuid.UUID] = _uuid_pk()
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    opened_by: Mapped[str] = mapped_column(String(64))

    #: Consumed on first successful pair. A window is one credential, not a
    #: standing invitation for its whole five minutes.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("paired_clients.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("expires_at > opened_at", name="expiry_after_opening"),
        CheckConstraint("(used_at IS NULL) = (used_by IS NULL)", name="used_pair_together"),
    )


class AlertEpisode(Base):
    """One threshold breach, from the reading that raised it to the one that
    cleared it.

    Named for the episode rather than the event, and deliberately not
    ``SensorAlert``: that name belongs to the wire model in
    ``bellasreef_contracts``, and the control engine imports both. One is a
    transition, the other is the span between two of them.

    A row is the *episode*, not the event: raised once, cleared once, never
    reopened. That makes "what is wrong right now" a `cleared_at IS NULL` scan
    instead of a fold over an event log, and it makes the duration of a breach a
    subtraction rather than a correlation.

    ``device_id`` is the subject token as a plain string, matching
    :class:`Override`. Deliberately no foreign key: an alert is a historical
    record of something that happened, and deleting the device should not erase
    the evidence that it misbehaved.
    """

    __tablename__ = "sensor_alerts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    sensor_type: Mapped[str] = mapped_column(String(64))
    bound: Mapped[str] = mapped_column(String(3))

    threshold: Mapped[float] = mapped_column(Float)
    clear_margin: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))

    raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    raised_value: Mapped[float] = mapped_column(Float)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_value: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint("bound IN ('min', 'max')", name="alert_bound_valid"),
        # Cleared means both halves recorded. A row with a clear time and no
        # clearing reading would render as "recovered to —".
        CheckConstraint(
            "(cleared_at IS NULL) = (cleared_value IS NULL)", name="cleared_pair_together"
        ),
        CheckConstraint(
            "cleared_at IS NULL OR cleared_at >= raised_at", name="cleared_after_raised"
        ),
        # At most one open episode per bound per device. The engine also holds
        # this in memory, but the engine restarts and the database does not:
        # without this index a restart mid-breach would open a second episode
        # and the tank would appear to be in breach twice.
        Index(
            "uq_sensor_alerts_one_active_per_bound",
            "device_id",
            "bound",
            unique=True,
            postgresql_where=text("cleared_at IS NULL"),
        ),
    )

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

    #: Where in the system this device physically is — "sump", "display left".
    #: Free text and nullable: it is for the operator's eyes, and a hub with one
    #: tank has nothing useful to put here.
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ---- binding: which capability channel this device is assigned to ----
    #
    # A device's identity (device_id, name, history) is independent of where it
    # is bound. Rebinding a light to different silicon changes these columns and
    # nothing else, so its subject, its alert history and its name all carry on.

    #: ``pi-pwm`` | ``pca9685`` | ``ds18b20``. NULL means announced but not yet
    #: adopted — a 1-Wire probe the hub can see and the operator has not claimed.
    driver_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Driver-specific binding, validated per driver at the API boundary.
    #: ``{"channel": 0}`` for pi-pwm; ``{"bus": 1, "address": 64, "channel": 3}``
    #: for pca9685; ``{"rom": "28-..."}`` for ds18b20.
    binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: True once the operator has claimed this device. An announced-unadopted
    #: device is visible through the API and inert: hardware-io builds no driver
    #: for it, so a probe appearing on the bus cannot start publishing under a
    #: name nobody chose.
    adopted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('sensor', 'actuator')", name="kind_valid"),
        CheckConstraint(
            "driver_type IS NULL OR driver_type IN ('pi-pwm', 'pca9685', 'ds18b20')",
            name="driver_type_valid",
        ),
        # Adopted means bound. The pair is what hardware-io keys on when it asks
        # the registry what to build, and an adopted device with no binding
        # would be an assignment to nowhere.
        CheckConstraint(
            "NOT adopted OR (driver_type IS NOT NULL AND binding IS NOT NULL)",
            name="adopted_devices_are_bound",
        ),
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
            )
            """,
            name="sensors_carry_no_control_authority",
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
        # §2.2 and §2.3: a device we cannot command must be unable to declare a
        # safe state, not merely have one ignored. A stored value is
        # indistinguishable from an enforced one to everything downstream, and
        # for observe_only there is no command path by which it could ever be
        # applied at all.
        CheckConstraint(
            """
            control_authority IS DISTINCT FROM 'advisory'
            AND control_authority IS DISTINCT FROM 'observe_only'
             OR safe_state IS NULL
            """,
            name="unenforceable_authority_declares_no_safe_state",
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
            AND transport       IS NOT NULL
            AND transport       IN ('local', 'network')
            )
            """,
            name="sensor_declares_type_cadence_and_transport",
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


class Capability(Base):
    """What a hardware source has announced it can do.

    Tier one of the registry. hardware-io announces these on startup — the PWM
    channels it can see, the 1-Wire bus, a PCA9685 if one is on the I²C bus —
    and the API stores them. This is what a "find devices" screen lists.

    Deliberately separate from :class:`Device`. A capability is a fact about the
    hardware: this hub has four PWM channels whether or not anybody has decided
    what they are for. A device is an operator's decision: *that* channel is the
    blue LED in the display tank. Conflating them is what makes a config file
    the source of truth and leaves the app with nothing to show until somebody
    edits YAML over SSH.

    Rows are replaced on each announcement rather than merged: what the hardware
    reports is the truth, and a capability that has gone away should stop being
    offered.
    """

    __tablename__ = "capabilities"

    id: Mapped[uuid.UUID] = _uuid_pk()

    #: ``pi-pwm`` | ``pca9685`` | ``w1-bus``.
    source: Mapped[str] = mapped_column(String(32), index=True)

    #: Stable identifier for this capability within its source, so a binding can
    #: name it: the channel number for PWM, the ROM for a 1-Wire probe.
    channel: Mapped[str] = mapped_column(String(64))

    #: Everything the announcement carried that a client might render: the GPIO
    #: a PWM channel reaches, the I²C address, the bus master name.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    announced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('pi-pwm', 'pca9685', 'w1-bus')", name="capability_source_valid"
        ),
        # One row per physical thing. A second announcement updates rather than
        # duplicates, so a hardware-io restart does not double the list.
        Index("uq_capabilities_source_channel", "source", "channel", unique=True),
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

    ``pairing_code`` is what the *operator* handles: six digits shown on the new
    device and typed into a paired one. The id and the code answer two different
    questions — the id is how the asking device follows its own request, the code
    is how a human points at it — and only the second can be read off a screen,
    which is why the v1 flow (approve a request by id) was uncompletable.
    """

    __tablename__ = "pairing_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    client_name: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), index=True)

    #: Nullable: rows created before migration 0016 have none, and a decided row
    #: keeps whatever it was shown with.
    pairing_code: Mapped[str | None] = mapped_column(String(6), nullable=True)

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
        CheckConstraint(
            "pairing_code IS NULL OR pairing_code ~ '^[0-9]{6}$'",
            name="pairing_code_digits",
        ),
        # Unique among requests *currently in play*, and only those. Six digits
        # are one namespace reused forever, so a plain UNIQUE would refuse a
        # request whose twin was approved last month. Partial, so the rule is
        # exactly the one the operator experiences: at any instant, one code
        # names one request.
        #
        # This is also why the sweeper has to write `expired`. A request that
        # aged out while still marked pending would hold its six digits out of
        # circulation forever.
        Index(
            "uq_pairing_requests_pending_code",
            "pairing_code",
            unique=True,
            postgresql_where=text("state = 'pending'"),
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

    #: How the engine moves the light to this level and back: "snap" (one
    #: step) or "ramp" (the global slew). Spec 2026-08-17. Governs both ends
    #: of the hold — arrival and release/expiry alike.
    transition: Mapped[str] = mapped_column(String(16), server_default=text("'ramp'"))

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
        CheckConstraint(
            "transition IN ('snap', 'ramp')",
            name="override_transition_valid",
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

    #: ``threshold`` or ``silence``. Two genuinely different emergencies: a
    #: probe reading 40 °C says the heater is stuck, a probe saying nothing says
    #: you do not know what the tank is doing. The second is worse and reads as
    #: calm on a dashboard, which is exactly why it needs its own class rather
    #: than a synthetic bound.
    alert_class: Mapped[str] = mapped_column(String(16), server_default=text("'threshold'"))

    #: Threshold episodes only. NULL on a silence, which has no side to be on.
    bound: Mapped[str | None] = mapped_column(String(3), nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    clear_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)

    raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    #: NULL on a silence: nothing was read, and that is the whole event.
    raised_value: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Silence episodes only. When the probe was last heard from, which is not
    #: ``raised_at`` — that is when we *noticed*, six cadences later. NULL for a
    #: probe that has not reported at all, where there is genuinely nothing to
    #: point at.
    last_reading_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_value: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint("alert_class IN ('threshold', 'silence')", name="alert_class_valid"),
        # A threshold episode carries its whole threshold story or it is not one.
        # Written as an implication rather than plain NOT NULL columns because
        # the same table now holds a class for which every one of these is
        # meaningless.
        CheckConstraint(
            "alert_class <> 'threshold' OR ("
            " bound IN ('min', 'max')"
            " AND threshold IS NOT NULL"
            " AND clear_margin IS NOT NULL"
            " AND unit IS NOT NULL"
            " AND raised_value IS NOT NULL)",
            name="threshold_episode_is_complete",
        ),
        # And a silence must not smuggle threshold fields in. Rejecting rather
        # than ignoring, the same rule device-classes.md §2.2 applies to a safe
        # state we could not honour: a value we would never render is a value
        # that would eventually be believed.
        CheckConstraint(
            "alert_class <> 'silence' OR ("
            " bound IS NULL"
            " AND threshold IS NULL"
            " AND clear_margin IS NULL"
            " AND raised_value IS NULL)",
            name="silence_episode_carries_no_threshold",
        ),
        # Cleared means both halves recorded. A row with a clear time and no
        # clearing reading would render as "recovered to —". This holds for
        # silence too: a silence clears on the first good reading, so there is
        # always a number to record.
        CheckConstraint(
            "(cleared_at IS NULL) = (cleared_value IS NULL)", name="cleared_pair_together"
        ),
        CheckConstraint(
            "cleared_at IS NULL OR cleared_at >= raised_at", name="cleared_after_raised"
        ),
        # At most one open episode per class per bound per device. The engine
        # also holds this in memory, but the engine restarts and the database
        # does not: without this a restart mid-breach would open a second
        # episode and the tank would appear to be in breach twice.
        #
        # NULLS NOT DISTINCT is load-bearing, not tidiness. A silence row has a
        # NULL bound, and under Postgres' default every NULL is distinct — so
        # without this the index would happily admit a hundred open silence
        # episodes for one probe while claiming to be unique.
        Index(
            "uq_sensor_alerts_one_active_per_class_bound",
            "device_id",
            "alert_class",
            "bound",
            unique=True,
            postgresql_where=text("cleared_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
    )


class HubIdentity(Base):
    """Who this hub is, decided once and never again.

    Written on first boot and then immutable. Before this, a backup manifest
    identified its origin by database host, database name and whichever machine
    ran the tool — enough to tell two hubs apart in practice, and not enough if
    they ever shared a hostname and a database name. Restoring the wrong
    archive onto a tank is not a mistake you want resting on a hostname.

    Single row, enforced by the database rather than by everyone remembering.
    ``singleton`` exists only to be the unique key: a partial index on a
    constant is the same trick with less to read.
    """

    __tablename__ = "hub_identity"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    singleton: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), unique=True)

    #: The current setup code, hashed the same way a refresh token is (see
    #: ``bellasreef_api.security.hash_setup_code``) — no plaintext at rest, so
    #: "I forgot" is answered by minting a new one, not by reading this back.
    #: NULL once setup is complete or before a code has ever been minted.
    setup_code_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: NULL means still in setup mode. Stamped once, on first successful
    #: pair by any method, and never unset again — a long-forgotten printed
    #: code must not quietly become a key again (spec 2026-08-15).
    setup_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (CheckConstraint("singleton", name="hub_identity_is_singleton"),)

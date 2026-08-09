"""schema v1: devices, calibration_records, dosing_journal, audit_log

Revision ID: 0001
Revises:
Create Date: 2026-08-09

Schema v1. Four tables, no speculative structures.

Two constraints carry real weight and are worth not "simplifying" later:

* ``ck_devices_actuator_declares_failure_behaviour`` is the storage-layer twin
  of the ActuatorRegistration model. An actuator with no declared safe state,
  runtime cap or heartbeat timeout cannot be persisted.

* ``audit_log`` gets a trigger that raises on UPDATE and DELETE. "Append-only"
  is enforced by the database, not by everyone remembering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.Column("sensor_type", sa.String(length=64), nullable=True),
        sa.Column("poll_interval_s", sa.Float(), nullable=True),
        sa.Column("actuator_class", sa.String(length=16), nullable=True),
        sa.Column("safe_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("max_runtime_s", sa.Float(), nullable=True),
        sa.Column("heartbeat_timeout_s", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('sensor', 'actuator')", name="kind_valid"),
        sa.CheckConstraint(
            "actuator_class IS NULL OR actuator_class IN ('binary', 'pwm')",
            name="actuator_class_valid",
        ),
        sa.CheckConstraint(
            "kind <> 'actuator' OR ("
            " actuator_class IS NOT NULL"
            " AND safe_state IS NOT NULL"
            " AND max_runtime_s IS NOT NULL AND max_runtime_s > 0"
            " AND heartbeat_timeout_s IS NOT NULL AND heartbeat_timeout_s > 0)",
            name="actuator_declares_failure_behaviour",
        ),
        sa.CheckConstraint(
            "kind <> 'sensor' OR (sensor_type IS NOT NULL AND poll_interval_s > 0)",
            name="sensor_declares_type_and_cadence",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_devices"),
        sa.UniqueConstraint("device_id", name="uq_devices_device_id"),
    )
    op.create_index("ix_devices_device_id", "devices", ["device_id"])

    op.create_table(
        "calibration_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("calibration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_pk", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("coefficients", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("residual", sa.Float(), nullable=False),
        sa.Column("points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("residual >= 0", name="residual_non_negative"),
        sa.ForeignKeyConstraint(
            ["device_pk"], ["devices.id"],
            name="fk_calibration_records_device_pk_devices",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_calibration_records"),
        sa.UniqueConstraint("calibration_id", name="uq_calibration_records_calibration_id"),
    )
    op.create_index("ix_calibration_records_device_pk", "calibration_records", ["device_pk"])

    op.create_table(
        "dosing_journal",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_pk", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("requested_ml", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("delivered_ml", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("container_level_ml_before", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("container_level_ml_after", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("intent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('intent', 'executing', 'confirmed', 'failed', 'aborted')",
            name="state_valid",
        ),
        sa.CheckConstraint("requested_ml > 0", name="requested_ml_positive"),
        sa.CheckConstraint(
            "delivered_ml IS NULL OR delivered_ml >= 0", name="delivered_ml_non_negative"
        ),
        sa.CheckConstraint(
            "state <> 'executing' OR executed_at IS NOT NULL", name="executing_has_timestamp"
        ),
        sa.CheckConstraint(
            "state <> 'confirmed' OR ("
            " executed_at IS NOT NULL"
            " AND confirmed_at IS NOT NULL"
            " AND delivered_ml IS NOT NULL)",
            name="confirmed_has_evidence",
        ),
        sa.CheckConstraint("state <> 'failed' OR error IS NOT NULL", name="failed_has_reason"),
        sa.ForeignKeyConstraint(
            ["device_pk"], ["devices.id"],
            name="fk_dosing_journal_device_pk_devices",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dosing_journal"),
        sa.UniqueConstraint("idempotency_key", name="uq_dosing_journal_idempotency_key"),
    )
    op.create_index("ix_dosing_journal_device_pk", "dosing_journal", ["device_pk"])
    op.create_index("ix_dosing_journal_state", "dosing_journal", ["state"])
    op.create_index("ix_dosing_journal_device_intent", "dosing_journal", ["device_pk", "intent_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column("event", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "category IN ('command', 'config', 'auth', 'state', 'safety', 'calibration')",
            name="category_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_category", "audit_log", ["category"])
    op.create_index("ix_audit_log_device_id", "audit_log", ["device_id"])

    # Append-only, enforced. Post-incident analysis is worthless if the log can
    # be edited after the incident.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bellasreef_audit_log_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only; % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_immutable
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION bellasreef_audit_log_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS bellasreef_audit_log_immutable();")
    op.drop_table("audit_log")
    op.drop_table("dosing_journal")
    op.drop_table("calibration_records")
    op.drop_table("devices")

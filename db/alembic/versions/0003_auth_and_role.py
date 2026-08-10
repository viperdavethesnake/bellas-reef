# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""auth tables and devices.role

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-09

Supports docs/contracts/auth.md (TOFU pairing, approve-from-paired, device-bound
refresh tokens) and the `role` field added in contracts 2.0.0.

Note on the role CHECK: `IS NOT NULL` comes first, for the same reason as the
constraints in 0001. A CHECK that evaluates to NULL PASSES in Postgres, so
`role IN (...)` alone would let an actuator with no role straight through.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES = "('light', 'heater', 'pump', 'doser', 'outlet')"


def upgrade() -> None:
    # --- devices.role -------------------------------------------------------
    op.add_column("devices", sa.Column("role", sa.String(length=16), nullable=True))
    # Existing actuator rows predate the column and would violate the CHECK.
    # 'outlet' is the honest default for an unclassified relay; a light would
    # have been registered as one.
    op.execute("UPDATE devices SET role = 'outlet' WHERE kind = 'actuator' AND role IS NULL")
    op.create_check_constraint(
        "actuator_declares_role",
        "devices",
        f"kind <> 'actuator' OR (role IS NOT NULL AND role IN {_ROLES})",
    )

    # --- paired_clients -----------------------------------------------------
    op.create_table(
        "paired_clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) <> (refresh_token_hash IS NULL)",
            name="revoked_iff_hash_cleared",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paired_clients"),
        sa.UniqueConstraint("refresh_token_hash", name="uq_paired_clients_refresh_token_hash"),
    )

    # --- signing_keys -------------------------------------------------------
    op.create_table(
        "signing_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("algorithm", sa.String(length=16), server_default="HS256", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_signing_keys"),
    )

    # --- pairing_requests ---------------------------------------------------
    op.create_table(
        "pairing_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_name", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_pk", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'denied', 'expired')", name="state_valid"
        ),
        sa.CheckConstraint(
            "state <> 'approved' OR (client_pk IS NOT NULL AND decided_at IS NOT NULL)",
            name="approved_has_client",
        ),
        sa.ForeignKeyConstraint(
            ["client_pk"], ["paired_clients.id"],
            name="fk_pairing_requests_client_pk_paired_clients",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pairing_requests"),
    )
    op.create_index("ix_pairing_requests_state", "pairing_requests", ["state"])


def downgrade() -> None:
    op.drop_table("pairing_requests")
    op.drop_table("signing_keys")
    op.drop_table("paired_clients")
    op.drop_constraint("ck_devices_actuator_declares_role", "devices", type_="check")
    op.drop_column("devices", "role")

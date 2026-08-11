# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""A hub knows who it is.

Closes the gap flagged when R14's manifest was written. Until now an archive
identified its origin by database host, database name, and whichever machine
happened to run the backup — which separates two hubs in practice and not at
all if they ever share a hostname and a database name. Restoring the wrong
archive onto a live tank should not come down to whether someone renamed a Pi.

The row is written on first boot by whichever service asks for it first, the
same lazy pattern as the JWT signing key, and never rewritten. Single row
enforced here rather than by everyone remembering: ``singleton`` exists only to
be a unique key that nothing else can occupy.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "hub_identity",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("singleton", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("singleton", name="hub_identity_is_singleton"),
        sa.UniqueConstraint("singleton", name="uq_hub_identity_singleton"),
    )

    # Deliberately not seeded here. A migration runs on every hub including the
    # one restoring a backup, and stamping an id at migration time would give a
    # restored database a *new* identity — losing exactly the fact the table
    # exists to preserve. The services write it on first boot instead, and a
    # restore brings the original row back with the rest of the data.


def downgrade() -> None:
    op.drop_table("hub_identity")

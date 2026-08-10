# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""audit_log.message_id, unique — exactly-once at rest

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-09

PRD R13 requires an append-only record of every command. One row per event is
the requirement; JetStream's at-least-once delivery produced two rows for a
redelivered event, which makes a query return the wrong count.

The fix pairs correctly with at-least-once: dedup at the terminal store on an
id the publisher stamps, rather than pretending the broker is exactly-once.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Added nullable first so existing rows survive, backfilled with fresh ids
    # (pre-existing rows have no envelope id to recover), then tightened. A
    # NOT NULL column added in one step would fail on a populated table.
    op.add_column(
        "audit_log",
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute("UPDATE audit_log SET message_id = gen_random_uuid() WHERE message_id IS NULL")
    op.alter_column("audit_log", "message_id", nullable=False)
    op.create_unique_constraint("uq_audit_log_message_id", "audit_log", ["message_id"])


def downgrade() -> None:
    op.drop_constraint("uq_audit_log_message_id", "audit_log", type_="unique")
    op.drop_column("audit_log", "message_id")

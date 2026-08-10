# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""pairing_windows: the `bellasreef pair` recovery path

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09

auth.md §1 recovery. A window row rather than clearing client state: deleting
revoked clients would reopen the TOFU-ever window, which is keyed on rows having
existed precisely so revoking everything cannot reopen open pairing. The
recovery path must not undo the thing it is recovering from.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pairing_windows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_by", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("expires_at > opened_at", name="expiry_after_opening"),
        sa.CheckConstraint("(used_at IS NULL) = (used_by IS NULL)", name="used_pair_together"),
        sa.ForeignKeyConstraint(
            ["used_by"], ["paired_clients.id"],
            name="fk_pairing_windows_used_by_paired_clients",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pairing_windows"),
    )
    op.create_index("ix_pairing_windows_expires_at", "pairing_windows", ["expires_at"])


def downgrade() -> None:
    op.drop_table("pairing_windows")

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""overrides: persisted manual holds with deadlines

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-09

docs/contracts/time-and-scheduling.md §4. Overrides are durations, not
schedules. expires_at is persisted only so an override can be lapsed or
re-armed across a restart; within a run the engine counts monotonic seconds.

The partial unique index is the "single active override per target" rule from
§4, enforced by the database rather than by every future caller remembering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("level", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=16), nullable=True),
        sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        sa.CheckConstraint(
            "(released_at IS NULL) = (release_reason IS NULL)",
            name="release_reason_iff_released",
        ),
        sa.CheckConstraint(
            "release_reason IS NULL OR release_reason IN "
            "('expired', 'lapsed', 'manual', 'superseded')",
            name="release_reason_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_overrides"),
    )
    op.create_index("ix_overrides_target", "overrides", ["target"])
    # One active override per target. A partial index, so released rows do not
    # collide with each other or with the next override on the same target.
    op.create_index(
        "uq_overrides_one_active_per_target",
        "overrides",
        ["target"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("overrides")

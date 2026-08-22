# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Chip state: one row per hardware source instance.

Storage-layer twin of ``bellasreef_contracts.messages.ChipState``
(docs/superpowers/specs/2026-08-19-chip-state-on-the-wire-design.md), which
hardware-io publishes on ``bellasreef.chip.<source>.<instance>`` after a chip
is brought up. Ruled 2026-08-18: option A — per-chip state gets its own
surface (the System → Hardware leaf), not a key in ``capabilities.detail``
(identity only, per #38) and not a field on the adopted device row.

``(source, instance)`` is the upsert target, not the primary key, so the
surrogate ``id`` survives whatever the API's upsert idiom needs — same shape
as ``capabilities``. The spec's own migration number ("0019") is superseded:
lighting schedules took 0019 first, so this is 0020.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "chip_state",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("instance", sa.String(64), nullable=False),
        sa.Column("initialised", sa.Boolean(), nullable=False),
        sa.Column("initialised_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "instance", name="uq_chip_state_source_instance"),
    )


def downgrade() -> None:
    op.drop_table("chip_state")

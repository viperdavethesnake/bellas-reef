# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Lighting schedules and their channel assignments.

A schedule is, in David's words, "a set of points from midnight to midnight,
for an individual PWM channel or group of PWM channels" — the storage-layer
twin of ``bellasreef_contracts.schedules.ScheduleDefinition``
(docs/superpowers/specs/2026-08-19-lighting-schedules-design.md, §Data
model). ``points`` is that model's wire shape verbatim, so a written schedule
and a read one are the same JSON with no lossy round trip.

``schedule_assignments`` names a deliberate choice: "assignment" already
belongs to the control engine's channel-adoption ``AssignmentLedger``, and
reusing the bare word here would collide two unrelated concepts under one
name. The table and its ORM class say ``schedule_assignments`` /
``ScheduleAssignment`` throughout, never plain "assignments".

``channel_id`` is the primary key rather than a unique index over a
surrogate id: one schedule per channel, and assigning a new one is meant to
*replace*, not add a row next to the old one. ``schedule_id`` is
``ON DELETE RESTRICT`` — deleting a schedule that is still driving a channel
is refused, not cascaded. This is the forgetDevice lesson (a referenced row
must be rejected, not 500'd — d2b35e3) applied here before a delete could
strand a channel silently.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "lighting_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("points", postgresql.JSONB(), nullable=False),
        sa.Column("zone", sa.String(64), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("anchor", sa.String(16), nullable=False, server_default=sa.text("'clock'")),
        sa.Column("locale", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_lighting_schedules_name"),
    )
    op.create_table(
        "schedule_assignments",
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["lighting_schedules.id"],
            name="fk_schedule_assignments_schedule_id_lighting_schedules",
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    op.drop_table("schedule_assignments")
    op.drop_table("lighting_schedules")

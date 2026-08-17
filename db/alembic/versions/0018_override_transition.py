# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Overrides carry how the light moves to them: snap or ramp.

Spec 2026-08-17 (hold transition). A hold is the operator standing at the
tank asking for a level; the engine's global slew (1 %/s) is right for a
schedule and wrong for that. ``transition`` is the operator's choice, per
hold, and governs both ends — arrival and release/expiry alike.

A first-class column rather than a key in ``level``: ``level`` mirrors the
wire ``PwmLevel``, and transition is how the engine moves *between* levels,
not a property of one. hardware-io never sees it.

Backfill: every existing row is ``ramp`` — the behaviour it was placed
under.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "overrides",
        sa.Column(
            "transition",
            sa.String(4),
            nullable=False,
            server_default=sa.text("'ramp'"),
        ),
    )
    op.create_check_constraint(
        "override_transition_valid", "overrides", "transition IN ('snap', 'ramp')"
    )


def downgrade() -> None:
    op.drop_constraint("override_transition_valid", "overrides", type_="check")
    op.drop_column("overrides", "transition")

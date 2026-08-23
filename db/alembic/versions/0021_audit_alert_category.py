# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""audit_log.category admits 'alert'.

The engine has published alert transitions on bellasreef.audit.alert since
threshold alerting shipped; the writer remapped them to 'safety' with
actor='hardware-io' because this CHECK predates the category. Post-incident
queries for category='alert' returned zero rows (2026-08-23 review, finding 10).

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.drop_constraint("category_valid", "audit_log", type_="check")
    op.create_check_constraint(
        "category_valid",
        "audit_log",
        "category IN ('command', 'config', 'auth', 'state', 'safety', 'calibration', 'alert')",
    )


def downgrade() -> None:
    # Refuses if 'alert' rows exist — append-only table, rows cannot be
    # rewritten, and silently re-filing them under 'safety' would repeat the
    # bug this migration fixes.
    op.drop_constraint("category_valid", "audit_log", type_="check")
    op.create_check_constraint(
        "category_valid",
        "audit_log",
        "category IN ('command', 'config', 'auth', 'state', 'safety', 'calibration')",
    )

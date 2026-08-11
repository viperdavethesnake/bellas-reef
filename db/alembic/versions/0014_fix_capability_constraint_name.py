# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Rename a double-prefixed constraint from 0013.

0013 named the capability source check ``ck_capabilities_capability_source_valid``
by hand, and the metadata naming convention (``ck_%(table_name)s_%(constraint_name)s``)
prefixed it again — so the database got
``ck_capabilities_ck_capabilities_capability_source_valid`` while the model
declares the single-prefixed form. The migration-drift test caught it in CI.

Fixed forward rather than by editing 0013, which has already been applied to the
live hub. Rewriting an applied migration leaves that hub permanently divergent
from a fresh install while both claim the same revision — the exact class of
silent disagreement the drift test exists to prevent.

``IF EXISTS`` on the drop: a database created after 0013 but before this fix has
the bad name, and one created from a corrected future baseline may not have it
at all. Both must survive this.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: None = None
depends_on: None = None

_BAD = "ck_capabilities_ck_capabilities_capability_source_valid"
_GOOD = "ck_capabilities_capability_source_valid"


def upgrade() -> None:
    op.execute(f"ALTER TABLE capabilities DROP CONSTRAINT IF EXISTS {_BAD}")
    op.execute(f"ALTER TABLE capabilities DROP CONSTRAINT IF EXISTS {_GOOD}")
    op.execute(
        f"ALTER TABLE capabilities ADD CONSTRAINT {_GOOD} "
        "CHECK (source IN ('pi-pwm', 'pca9685', 'w1-bus'))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE capabilities DROP CONSTRAINT IF EXISTS {_GOOD}")
    op.execute(
        f"ALTER TABLE capabilities ADD CONSTRAINT {_BAD} "
        "CHECK (source IN ('pi-pwm', 'pca9685', 'w1-bus'))"
    )

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Setup code + setup-completed marker on hub_identity.

Spec 2026-08-15, Feature 1: a new hub prints an 8-character setup code
instead of forcing a first pairing through the approval flow that has
nobody yet to approve it. ``setup_code_hash`` carries the current code,
hashed the same way a refresh token is (see
``bellasreef_api.security.hash_setup_code``) — no plaintext at rest, so
"I forgot" is answered by minting a new one, not by reading the old one
back. ``setup_completed_at`` is the setup-mode gate itself: NULL means
still in setup mode, and it is stamped once, on first successful pair, by
any method, and never unset again.

Backfill: any hub that has ever paired a client is already set up and must
never re-enter setup mode (spec: a long-forgotten printed code must not
quietly become a key again).

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column("hub_identity", sa.Column("setup_code_hash", sa.Text(), nullable=True))
    op.add_column(
        "hub_identity",
        sa.Column("setup_completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE hub_identity SET setup_completed_at = now() "
        "WHERE EXISTS (SELECT 1 FROM paired_clients)"
    )


def downgrade() -> None:
    op.drop_column("hub_identity", "setup_completed_at")
    op.drop_column("hub_identity", "setup_code_hash")

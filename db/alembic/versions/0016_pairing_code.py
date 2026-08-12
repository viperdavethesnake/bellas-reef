# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""A pairing request carries six digits the operator can read off a screen.

auth.md v2 §2 step 3: second-device pairing becomes code-based. The new device
displays ``pairing_code`` and the operator types it into an already-paired
device, which calls ``POST /api/v1/pair/claim``. v1's approve-from-paired flow
was never completable — no client could obtain a ``request_id`` — so the code is
what makes the journey reachable at all.

Uniqueness is a **partial unique index over pending requests**, not a retry loop
in application code. Two reasons it has to be the index. Codes are reused over
time by construction (six digits, one namespace, forever), so a plain UNIQUE
would refuse a request whose twin was approved last month; and a uniqueness rule
enforced by whoever remembers to check is a rule that holds until the second
writer.

The column is nullable because history has no codes and there is nothing to
derive one from. Existing *pending* rows are aged out here rather than left
behind: a pending request with a NULL code is unclaimable through the new flow,
so leaving it pending would be a request that can never be decided. A device
polling one gets 410 and starts again, which is the recovery it already
implements.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "pairing_requests",
        sa.Column("pairing_code", sa.String(length=6), nullable=True),
    )

    # Six digits, zero-padded. Stored as text rather than an integer precisely
    # so "042913" survives the round trip — an integer column would hand the
    # client 42913 and the operator would type five digits into a six-digit
    # field.
    op.create_check_constraint(
        "pairing_code_digits",
        "pairing_requests",
        "pairing_code IS NULL OR pairing_code ~ '^[0-9]{6}$'",
    )

    # Unique among requests currently in play, and only those.
    op.create_index(
        "uq_pairing_requests_pending_code",
        "pairing_requests",
        ["pairing_code"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )

    # In-flight requests predate the code and cannot be claimed with one.
    op.execute("UPDATE pairing_requests SET state = 'expired' WHERE state = 'pending'")


def downgrade() -> None:
    op.drop_index("uq_pairing_requests_pending_code", table_name="pairing_requests")
    op.drop_constraint(
        "ck_pairing_requests_pairing_code_digits", "pairing_requests", type_="check"
    )
    op.drop_column("pairing_requests", "pairing_code")

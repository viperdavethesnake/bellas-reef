# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""devices.display_name: what the operator calls a device

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-10

Nullable, and deliberately never backfilled from ``device_id``. A NULL here
means "the operator has not named this", which is exactly what a client needs
in order to decide whether to show a name or fall back to the id. Copying the
id in at migration time would erase that distinction permanently and leave
every probe looking as though somebody had chosen to call it
``ds18b20-28-000000bfe244``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("display_name", sa.String(length=128), nullable=True))
    op.create_check_constraint(
        "display_name_not_blank",
        "devices",
        "display_name IS NULL OR length(btrim(display_name)) > 0",
    )


def downgrade() -> None:
    op.drop_constraint("display_name_not_blank", "devices", type_="check")
    op.drop_column("devices", "display_name")

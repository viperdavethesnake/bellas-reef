# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""devices: observe_only may not declare a safe state either

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-10

docs/device-classes.md §2.3, closing a gap left open by 0008. That migration
enforced the rule for ``advisory`` only, because §2.3 did not state it at the
time; the reasoning is if anything stronger for ``observe_only``, since no
command is ever emitted to such a device and therefore no path exists by which a
safe state could ever be applied.

Widening the existing constraint rather than adding a second one, so there is a
single place that answers "which authorities may carry a safe state".
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNENFORCEABLE = (
    "control_authority IS DISTINCT FROM 'advisory' "
    "AND control_authority IS DISTINCT FROM 'observe_only' "
    "OR safe_state IS NULL"
)


def upgrade() -> None:
    op.drop_constraint("advisory_declares_no_safe_state", "devices", type_="check")
    op.create_check_constraint(
        "unenforceable_authority_declares_no_safe_state", "devices", _UNENFORCEABLE
    )


def downgrade() -> None:
    op.drop_constraint(
        "unenforceable_authority_declares_no_safe_state", "devices", type_="check"
    )
    op.create_check_constraint(
        "advisory_declares_no_safe_state",
        "devices",
        "control_authority IS DISTINCT FROM 'advisory' OR safe_state IS NULL",
    )

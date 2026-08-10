# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""devices: per-sensor alert thresholds (PRD R12)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-09

Columns on ``devices`` rather than a side table: a threshold has exactly the
lifetime of the device it describes, and a join buys nothing when the
cardinality is one-to-one and the delete cascade is the behaviour we want
anyway.

All three columns are nullable. A sensor with no thresholds is not evaluated,
and that is the default — alerting is opt-in per device, so adding this
migration cannot start paging anyone about a tank that was fine yesterday.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("alert_min", sa.Float(), nullable=True))
    op.add_column("devices", sa.Column("alert_max", sa.Float(), nullable=True))
    op.add_column("devices", sa.Column("alert_clear_margin", sa.Float(), nullable=True))

    # Every constraint below leads with IS NULL. This is the third time this
    # pattern has been load-bearing in this schema: in Postgres a CHECK that
    # evaluates to NULL *passes*, so `alert_clear_margin > 0` alone would admit
    # a NULL margin without complaint.
    op.create_check_constraint(
        "thresholds_are_sensor_only",
        "devices",
        """
        kind = 'sensor' OR (
            alert_min          IS NULL
        AND alert_max          IS NULL
        AND alert_clear_margin IS NULL
        )
        """,
    )
    op.create_check_constraint(
        "clear_margin_positive",
        "devices",
        "alert_clear_margin IS NULL OR alert_clear_margin > 0",
    )
    op.create_check_constraint(
        "alert_band_ordered",
        "devices",
        "alert_min IS NULL OR alert_max IS NULL OR alert_min < alert_max",
    )
    op.create_check_constraint(
        "thresholds_require_clear_margin",
        "devices",
        """
        (alert_min IS NULL AND alert_max IS NULL)
        OR alert_clear_margin IS NOT NULL
        """,
    )
    # With both bounds set the clear zone is [min + margin, max - margin]. A
    # margin wider than half the band makes that interval empty: the reading is
    # either in breach or not yet cleared, forever, and the alert latches with
    # no way out short of editing config. Refused at rest.
    op.create_check_constraint(
        "clear_zone_is_reachable",
        "devices",
        """
        alert_min IS NULL OR alert_max IS NULL OR alert_clear_margin IS NULL
        OR (alert_min + alert_clear_margin) < (alert_max - alert_clear_margin)
        """,
    )


    op.create_table(
        "sensor_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("sensor_type", sa.String(length=64), nullable=False),
        sa.Column("bound", sa.String(length=3), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("clear_margin", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("raised_value", sa.Float(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_value", sa.Float(), nullable=True),
        sa.CheckConstraint("bound IN ('min', 'max')", name="alert_bound_valid"),
        sa.CheckConstraint(
            "(cleared_at IS NULL) = (cleared_value IS NULL)", name="cleared_pair_together"
        ),
        sa.CheckConstraint(
            "cleared_at IS NULL OR cleared_at >= raised_at", name="cleared_after_raised"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sensor_alerts"),
    )
    op.create_index("ix_sensor_alerts_device_id", "sensor_alerts", ["device_id"])
    op.create_index("ix_sensor_alerts_raised_at", "sensor_alerts", ["raised_at"])
    # One open episode per bound per device. The engine keeps the same invariant
    # in memory; this is the half that survives a restart mid-breach.
    op.create_index(
        "uq_sensor_alerts_one_active_per_bound",
        "sensor_alerts",
        ["device_id", "bound"],
        unique=True,
        postgresql_where=sa.text("cleared_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("sensor_alerts")
    op.drop_constraint("clear_zone_is_reachable", "devices", type_="check")
    op.drop_constraint("thresholds_require_clear_margin", "devices", type_="check")
    op.drop_constraint("alert_band_ordered", "devices", type_="check")
    op.drop_constraint("clear_margin_positive", "devices", type_="check")
    op.drop_constraint("thresholds_are_sensor_only", "devices", type_="check")
    op.drop_column("devices", "alert_clear_margin")
    op.drop_column("devices", "alert_max")
    op.drop_column("devices", "alert_min")

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Silence is its own alert class, and can coexist with a breach.

A probe that goes quiet mid-breach is not hypothetical: it is exactly what
2026-08-10 produced, and it is the case that forces the two classes apart. The
old uniqueness rule was one open episode per (device, bound), which cannot
express "too cold AND not reporting" at the same time.

So the key gains the class. The threshold columns become nullable, because a
silence has no bound, no threshold and no reading — that absence *is* the
event — and two implication constraints keep each class honest about the
columns it does and does not own.

``NULLS NOT DISTINCT`` on the new index is load-bearing rather than tidy. A
silence row has a NULL bound, and under Postgres' default rule every NULL is
distinct from every other, so the index would have admitted unlimited open
silence episodes for one probe while still calling itself unique.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "sensor_alerts",
        sa.Column(
            "alert_class",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'threshold'"),
        ),
    )
    op.add_column(
        "sensor_alerts",
        sa.Column("last_reading_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Every existing row is a threshold episode, which is what the server
    # default already said. Kept as a real default rather than dropped: the
    # engine names the class explicitly on insert, and if a future writer
    # forgets, landing in the class that carries constraints is the safer
    # accident than landing in one that does not.

    for column in ("bound", "threshold", "clear_margin", "unit", "raised_value"):
        op.alter_column("sensor_alerts", column, nullable=True)

    # The old blanket bound check no longer holds: a silence has no bound. Its
    # job moves into the class-scoped constraint below.
    op.drop_constraint("alert_bound_valid", "sensor_alerts", type_="check")

    op.create_check_constraint(
        "alert_class_valid",
        "sensor_alerts",
        "alert_class IN ('threshold', 'silence')",
    )
    op.create_check_constraint(
        "threshold_episode_is_complete",
        "sensor_alerts",
        "alert_class <> 'threshold' OR ("
        " bound IN ('min', 'max')"
        " AND threshold IS NOT NULL"
        " AND clear_margin IS NOT NULL"
        " AND unit IS NOT NULL"
        " AND raised_value IS NOT NULL)",
    )
    op.create_check_constraint(
        "silence_episode_carries_no_threshold",
        "sensor_alerts",
        "alert_class <> 'silence' OR ("
        " bound IS NULL"
        " AND threshold IS NULL"
        " AND clear_margin IS NULL"
        " AND raised_value IS NULL)",
    )

    op.drop_index("uq_sensor_alerts_one_active_per_bound", table_name="sensor_alerts")
    op.execute(
        "CREATE UNIQUE INDEX uq_sensor_alerts_one_active_per_class_bound "
        "ON sensor_alerts (device_id, alert_class, bound) NULLS NOT DISTINCT "
        "WHERE cleared_at IS NULL"
    )


def downgrade() -> None:
    """Refuses while any silence episode exists.

    Downgrading would have to either delete those rows or force them into a
    bound they never had. Both destroy the record of a probe having been dead,
    which is the one thing the class was added to preserve.
    """
    silences = op.get_bind().execute(
        sa.text("SELECT count(*) FROM sensor_alerts WHERE alert_class = 'silence'")
    ).scalar_one()
    if silences:
        raise RuntimeError(
            f"{silences} silence episode(s) exist. Downgrading would erase the record of a "
            "probe having stopped reporting; delete them deliberately first if that is "
            "really what you want."
        )

    op.drop_index("uq_sensor_alerts_one_active_per_class_bound", table_name="sensor_alerts")
    op.drop_constraint("silence_episode_carries_no_threshold", "sensor_alerts", type_="check")
    op.drop_constraint("threshold_episode_is_complete", "sensor_alerts", type_="check")
    op.drop_constraint("alert_class_valid", "sensor_alerts", type_="check")

    for column in ("bound", "threshold", "clear_margin", "unit", "raised_value"):
        op.alter_column("sensor_alerts", column, nullable=False)

    op.create_check_constraint(
        "alert_bound_valid", "sensor_alerts", "bound IN ('min', 'max')"
    )
    op.create_index(
        "uq_sensor_alerts_one_active_per_bound",
        "sensor_alerts",
        ["device_id", "bound"],
        unique=True,
        postgresql_where=sa.text("cleared_at IS NULL"),
    )
    op.drop_column("sensor_alerts", "last_reading_at")
    op.drop_column("sensor_alerts", "alert_class")

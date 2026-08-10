# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""devices: control authority, failsafe capability, transport

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-10

docs/device-classes.md §2 and §6.

The columns are additive; the *meaning* of the existing rows is not. Every
actuator registered before this migration asserted the R1 safety triple, so
every one of them becomes ``authoritative`` / ``true`` / ``local`` — accurate
today, since everything we drive is a PCA9685 channel or a GPIO pin.

The reason this lands before the telemetry writer rather than after is in §4:
VictoriaMetrics series identity is its label set, and ``control_authority`` has
to be on the first sample ever written. Added later it forks every series, and a
duty of 0.6 that was a measurement becomes indistinguishable from one that was a
request nobody acknowledged.

**This migration rescopes a P0 constraint.** ``actuator_declares_failure_behaviour``
required the safety triple of every actuator; it is replaced by
``authoritative_declares_failure_behaviour``, which requires it of authoritative
devices only, per PRD R1 as amended. For every row that exists today the two
rules are identical, because every row is authoritative.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("control_authority", sa.String(length=16), nullable=True))
    op.add_column("devices", sa.Column("failsafe_capable", sa.Boolean(), nullable=True))
    op.add_column("devices", sa.Column("transport", sa.String(length=8), nullable=True))

    # §6: backfill before the constraints land, or the constraints reject the
    # rows that are already there.
    op.execute(
        """
        UPDATE devices
           SET control_authority = 'authoritative',
               failsafe_capable  = true,
               transport         = 'local'
         WHERE kind = 'actuator'
        """
    )

    op.drop_constraint("actuator_declares_failure_behaviour", "devices", type_="check")

    op.create_check_constraint(
        "actuator_declares_authority",
        "devices",
        """
        kind <> 'actuator' OR (
            actuator_class    IS NOT NULL
        AND control_authority IS NOT NULL
        AND control_authority IN ('authoritative', 'advisory', 'observe_only')
        AND failsafe_capable  IS NOT NULL
        AND transport         IS NOT NULL
        AND transport         IN ('local', 'network')
        )
        """,
    )
    op.create_check_constraint(
        "sensors_carry_no_authority",
        "devices",
        """
        kind <> 'sensor' OR (
            control_authority IS NULL
        AND failsafe_capable  IS NULL
        AND transport         IS NULL
        )
        """,
    )
    # `IS DISTINCT FROM`, not `<>`: on a sensor row control_authority is NULL,
    # `NULL <> 'authoritative'` is NULL, and a CHECK evaluating to NULL passes —
    # which would be harmless here but is the same trap that has already been
    # load-bearing four times in this schema. Written the safe way by default.
    op.create_check_constraint(
        "authoritative_declares_failure_behaviour",
        "devices",
        """
        control_authority IS DISTINCT FROM 'authoritative' OR (
            failsafe_capable    IS TRUE
        AND transport           = 'local'
        AND safe_state          IS NOT NULL
        AND max_runtime_s       IS NOT NULL AND max_runtime_s > 0
        AND heartbeat_timeout_s IS NOT NULL AND heartbeat_timeout_s > 0
        )
        """,
    )
    op.create_check_constraint(
        "advisory_declares_no_safe_state",
        "devices",
        "control_authority IS DISTINCT FROM 'advisory' OR safe_state IS NULL",
    )


def downgrade() -> None:
    # Downgrading is only safe while nothing non-authoritative exists, because
    # the restored constraint demands a safety triple of every actuator. Refuse
    # rather than corrupt: dropping the columns would erase the only record of
    # which guarantee a row was asserting.
    conn = op.get_bind()
    offenders = conn.execute(
        sa.text(
            "SELECT count(*) FROM devices "
            "WHERE kind = 'actuator' AND control_authority IS DISTINCT FROM 'authoritative'"
        )
    ).scalar_one()
    if offenders:
        raise RuntimeError(
            f"{offenders} non-authoritative actuator(s) exist; downgrading would "
            "restore a constraint they cannot satisfy and discard their authority"
        )

    op.drop_constraint("advisory_declares_no_safe_state", "devices", type_="check")
    op.drop_constraint("authoritative_declares_failure_behaviour", "devices", type_="check")
    op.drop_constraint("sensors_carry_no_authority", "devices", type_="check")
    op.drop_constraint("actuator_declares_authority", "devices", type_="check")
    op.create_check_constraint(
        "actuator_declares_failure_behaviour",
        "devices",
        """
        kind <> 'actuator' OR (
            actuator_class      IS NOT NULL
        AND safe_state          IS NOT NULL
        AND max_runtime_s       IS NOT NULL AND max_runtime_s > 0
        AND heartbeat_timeout_s IS NOT NULL AND heartbeat_timeout_s > 0
        )
        """,
    )
    op.drop_column("devices", "transport")
    op.drop_column("devices", "failsafe_capable")
    op.drop_column("devices", "control_authority")

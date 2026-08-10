# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""devices: sensors declare a transport

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-10

An alert episode is labelled with the transport of the device that produced it,
so the device row has to carry one. Migration 0008 forbade every authority
column on a sensor, transport included; that was right for ``control_authority``
— a sensor controls nothing — and wrong for ``transport``, which is provenance
rather than authority.

The distinction is the point of the label. A 1-Wire probe on the board is a
measurement; a value relayed from a vendor's cloud is a report. An alert raised
from each is a different kind of claim, and after the first vendor bridge ships
they are indistinguishable in history unless the label is on the series from the
start.

Existing sensors backfill to ``local``: everything registered today is a
DS18B20 on the board's own 1-Wire bus.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("sensors_carry_no_authority", "devices", type_="check")
    op.execute("UPDATE devices SET transport = 'local' WHERE kind = 'sensor'")

    op.create_check_constraint(
        "sensors_carry_no_control_authority",
        "devices",
        """
        kind <> 'sensor' OR (
            control_authority IS NULL
        AND failsafe_capable  IS NULL
        )
        """,
    )
    op.drop_constraint("sensor_declares_type_and_cadence", "devices", type_="check")
    op.create_check_constraint(
        "sensor_declares_type_cadence_and_transport",
        "devices",
        """
        kind <> 'sensor' OR (
            sensor_type     IS NOT NULL
        AND poll_interval_s IS NOT NULL AND poll_interval_s > 0
        AND transport       IS NOT NULL
        AND transport       IN ('local', 'network')
        )
        """,
    )


def downgrade() -> None:
    op.drop_constraint("sensor_declares_type_cadence_and_transport", "devices", type_="check")
    op.create_check_constraint(
        "sensor_declares_type_and_cadence",
        "devices",
        """
        kind <> 'sensor' OR (
            sensor_type     IS NOT NULL
        AND poll_interval_s IS NOT NULL AND poll_interval_s > 0
        )
        """,
    )
    op.drop_constraint("sensors_carry_no_control_authority", "devices", type_="check")
    op.execute("UPDATE devices SET transport = NULL WHERE kind = 'sensor'")
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

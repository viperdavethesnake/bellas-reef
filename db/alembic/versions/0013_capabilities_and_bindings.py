# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The registry becomes the source of device topology.

Two tiers, and keeping them apart is the point.

**Capabilities** are announced by hardware-io: the PWM channels it can see, the
1-Wire bus, a PCA9685 if one answers on I²C. Facts about the hardware, true
whether or not anybody has decided what they are for. This is what a "find
devices" screen lists.

**Devices** are the operator's decisions, and gain the columns that record one:
``driver_type`` and ``binding`` say which capability channel this device is
assigned to, ``location`` says where it physically is, and ``adopted`` says
whether a human has claimed it yet.

The split is what a config file could not express. A YAML file conflates "this
hub has four PWM channels" with "channel 0 is the blue LED", so the app has
nothing to show until somebody edits YAML over SSH, and a probe appearing on the
bus either starts publishing under a name nobody chose or stays invisible.

Announced-but-unadopted is the state that fixes it: visible through the API,
inert until named.

Identity stays independent of binding. Rebinding a light to different silicon
rewrites ``driver_type`` and ``binding`` and touches nothing else, so its
subject, its history and its name carry on.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "capabilities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "announced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source IN ('pi-pwm', 'pca9685', 'w1-bus')",
            name="ck_capabilities_capability_source_valid",
        ),
    )
    op.create_index("ix_capabilities_source", "capabilities", ["source"])
    # One row per physical thing: a hardware-io restart updates rather than
    # doubling the list.
    op.create_index(
        "uq_capabilities_source_channel", "capabilities", ["source", "channel"], unique=True
    )

    op.add_column("devices", sa.Column("location", sa.String(length=128), nullable=True))
    op.add_column("devices", sa.Column("driver_type", sa.String(length=32), nullable=True))
    op.add_column(
        "devices",
        sa.Column("binding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column(
            "adopted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )

    op.create_check_constraint(
        "driver_type_valid",
        "devices",
        "driver_type IS NULL OR driver_type IN ('pi-pwm', 'pca9685', 'ds18b20')",
    )
    op.create_check_constraint(
        "adopted_devices_are_bound",
        "devices",
        "NOT adopted OR (driver_type IS NOT NULL AND binding IS NOT NULL)",
    )

    # Existing devices predate the registry and were built from a config file,
    # so they are already in service. Marking them adopted preserves that:
    # the alternative would make a running hub's probe go inert on upgrade,
    # which is a migration that takes the tank offline.
    #
    # Their binding is filled in from what hardware-io announces on next start,
    # so this only claims they are the operator's, not where they are wired.
    op.execute(
        """
        UPDATE devices
           SET driver_type = 'ds18b20',
               binding = jsonb_build_object('rom', device_id),
               adopted = true
         WHERE kind = 'sensor'
           AND sensor_type = 'temp'
           AND driver_type IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_devices_adopted_devices_are_bound", "devices", type_="check")
    op.drop_constraint("ck_devices_driver_type_valid", "devices", type_="check")
    op.drop_column("devices", "adopted")
    op.drop_column("devices", "binding")
    op.drop_column("devices", "driver_type")
    op.drop_column("devices", "location")
    op.drop_index("uq_capabilities_source_channel", table_name="capabilities")
    op.drop_index("ix_capabilities_source", table_name="capabilities")
    op.drop_table("capabilities")

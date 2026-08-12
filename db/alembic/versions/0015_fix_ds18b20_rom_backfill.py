# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""0013 backfilled the wrong ROM.

It wrote ``binding = {'rom': device_id}``, and a DS18B20's device_id is not its
ROM: the live hub's probe is ``ds18b20-28-000000bfe244`` while its ROM is
``28-000000bfe244``. Every hub applying 0013 gets a binding pointing at a probe
that does not exist.

Worse than cosmetic, because the binding is what identifies hardware. An import
that matches an existing device by ROM — which is the fix for the identity fork
this backfill helped cause — would find nothing and create a parallel row for a
probe already in the registry.

Extracts the real ROM from the device_id where one is recognisable. A row whose
device_id carries no ``28-xxxxxxxxxxxx`` is left alone and its binding nulled:
there is nothing to derive from, and a wrong ROM is worse than an absent one —
absent asks the operator to bind it, wrong silently binds it to the wrong probe.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    # Where the device_id contains a recognisable ROM, use it.
    op.execute(
        """
        UPDATE devices
           SET binding = jsonb_build_object(
                   'rom', substring(device_id from '28-[0-9a-f]{12}')
               )
         WHERE driver_type = 'ds18b20'
           AND device_id ~ '28-[0-9a-f]{12}'
           AND COALESCE(binding ->> 'rom', '') !~ '^28-[0-9a-f]{12}$'
        """
    )

    # Where it does not, refuse to guess. Unadopt as well: an adopted device
    # with no binding violates the adopted_devices_are_bound CHECK, and leaving
    # it adopted-but-unbound would fail the constraint rather than the operator.
    op.execute(
        """
        UPDATE devices
           SET binding = NULL,
               adopted = false
         WHERE driver_type = 'ds18b20'
           AND COALESCE(binding ->> 'rom', '') !~ '^28-[0-9a-f]{12}$'
        """
    )


def downgrade() -> None:
    """Deliberately not reinstated.

    The old value was wrong. Writing it back would restore a binding that names
    a probe which does not exist, and nothing downstream is better off for it.
    """

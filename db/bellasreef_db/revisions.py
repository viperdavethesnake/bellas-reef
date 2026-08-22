# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The migration revisions this build of the code knows about.

Restore compares an archive's stamped revision against this tuple. A revision
that is not in it came from a newer hub, and the dump it belongs to describes a
schema this code has never seen — so the restore is refused rather than
attempted. That check is the reason the list has to exist as *runtime data*:
``db/alembic`` is a build-time directory, not part of the ``bellasreef-db``
wheel, so a deployed container cannot read the migration files to find out what
it knows.

Written by hand, kept honest by ``db/tests/test_revisions.py``, which fails the
build if a migration lands without being listed here. Adding a migration means
adding a line below — and the test names the revision you forgot.
"""

from __future__ import annotations

from typing import Final

#: Oldest first, matching the order Alembic walks the chain.
KNOWN_REVISIONS: Final[tuple[str, ...]] = (
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "0011",
    "0012",
    "0013",
    "0014",
    "0015",
    "0016",
    "0017",
    "0018",
    "0019",
    "0020",
)

#: What a hub running this code stamps into ``alembic_version`` once migrated.
HEAD_REVISION: Final[str] = KNOWN_REVISIONS[-1]

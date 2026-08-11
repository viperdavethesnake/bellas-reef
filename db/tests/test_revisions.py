# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The revision list a *binary* knows, kept honest against the migration files.

Restore has to answer one question before it touches a database: was this
archive written by a newer hub than the one being restored onto? The answer is
the alembic revision stamped in the archive's manifest — if this binary has
never heard of it, the archive is from the future and its dump contains tables
and constraints this code cannot reason about.

Answering it needs the migration graph available *as data at runtime*, and the
alembic directory is not in the ``bellasreef-db`` wheel — only the
``bellasreef_db`` package is. So the list is written out in
:mod:`bellasreef_db.revisions`, and this test is what stops it drifting from the
files it claims to describe. Same shape as the hand-written migration 0001 and
its drift test: state the fact once, then forbid it going stale.

No database. This compares two things on disk.
"""

from __future__ import annotations

from pathlib import Path

from alembic.script import ScriptDirectory
from bellasreef_db.revisions import HEAD_REVISION, KNOWN_REVISIONS

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent / "alembic"


def _revisions_on_disk() -> tuple[str, ...]:
    """Every revision in the migration files, oldest first."""
    script = ScriptDirectory(str(_ALEMBIC_DIR))
    # walk_revisions() yields newest -> oldest.
    return tuple(reversed([s.revision for s in script.walk_revisions()]))


def test_known_revisions_matches_the_migration_files() -> None:
    assert KNOWN_REVISIONS == _revisions_on_disk(), (
        "bellasreef_db.revisions.KNOWN_REVISIONS has drifted from db/alembic/versions. "
        "A new migration must be added to that tuple, or restore will reject archives "
        "taken from a hub running this very code."
    )


def test_head_revision_is_the_last_known_one() -> None:
    assert HEAD_REVISION == KNOWN_REVISIONS[-1]

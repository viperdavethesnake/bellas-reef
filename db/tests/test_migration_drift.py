"""Migration/model drift.

Migration 0001 is hand-written. Hand-written migrations drift from the models
they are supposed to create, and the drift is invisible until a deploy builds a
schema that does not match what the code expects.

This applies every migration to a real database and asks Alembic whether the
resulting schema still differs from the declarative metadata. Any difference is
a failure.
"""

from __future__ import annotations

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from bellasreef_db.models import Base
from helpers import engine, requires_postgres, run
from sqlalchemy import Connection

pytestmark = requires_postgres

#: Objects Alembic reports that are not ours to own.
_IGNORED_TABLES = {"alembic_version"}


def _diff_sync(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True},
    )
    diffs = compare_metadata(context, Base.metadata)
    kept: list[Any] = []
    for d in diffs:
        # Diff entries are tuples whose first element is the operation name;
        # table-level ops carry the table object at a known position.
        if isinstance(d, tuple) and d and isinstance(d[0], str):
            name = d[0]
            if name in {"remove_table", "add_table"}:
                table = d[1]
                if getattr(table, "name", None) in _IGNORED_TABLES:
                    continue
        kept.append(d)
    return kept


def test_schema_matches_models_after_migration() -> None:
    """Run the migrations, then assert nothing is left to autogenerate."""

    async def check() -> list[Any]:
        eng = engine()
        try:
            async with eng.connect() as conn:
                return await conn.run_sync(_diff_sync)
        finally:
            await eng.dispose()

    diffs = run(check)
    assert diffs == [], (
        f"migration 0001 and bellasreef_db.models have drifted. Alembic would generate: {diffs!r}"
    )

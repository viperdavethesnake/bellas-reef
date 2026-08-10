# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Alembic environment.

Async because the services are async — using a sync driver here would mean two
Postgres drivers in the image for no reason. Pattern follows the Alembic
cookbook's asyncio recipe: an async engine, with the migration body run through
``connection.run_sync``.

The DSN is read from ``BELLASREEF_DATABASE_URL`` and never stored in
``alembic.ini``.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from bellasreef_db.models import Base
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_DSN_ENV = "BELLASREEF_DATABASE_URL"


def _database_url() -> str:
    url = os.environ.get(_DSN_ENV)
    if not url:
        raise RuntimeError(
            f"{_DSN_ENV} is not set. Example: "
            "postgresql+asyncpg://bellasreef:***@localhost:5432/bellasreef"
        )
    return url


def run_migrations_offline() -> None:
    """Render migrations as SQL without a live database.

    Used by CI to prove the migration scripts are valid without standing up
    Postgres.
    """
    context.configure(
        url=os.environ.get(_DSN_ENV, "postgresql+asyncpg://offline/offline"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

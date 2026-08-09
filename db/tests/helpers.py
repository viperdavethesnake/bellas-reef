"""Postgres-backed test helpers.

These need a real database: the constraints under test are Postgres semantics,
not SQLAlchemy ones, and the NULL-passes-a-CHECK behaviour that motivated them
cannot be reproduced against SQLite or a mock. That is the whole point — a test
that did not use Postgres would have happily passed the buggy constraint.

Skipped unless ``BELLASREEF_TEST_DATABASE_URL`` is set. CI sets it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_ENV = "BELLASREEF_TEST_DATABASE_URL"

requires_postgres = pytest.mark.skipif(
    not os.environ.get(_ENV),
    reason=f"{_ENV} not set; these assert real Postgres constraint semantics",
)


def database_url() -> str:
    url = os.environ.get(_ENV)
    if not url:
        pytest.skip(f"{_ENV} not set")
    return url


def run[T](coro: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(coro())


def engine() -> AsyncEngine:
    return create_async_engine(database_url(), future=True)

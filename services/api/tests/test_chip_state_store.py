# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Chip-state store: upsert per ``(source, instance)``, ordered list.

Storage-layer twin of the API's Task 2 (PR2 plan,
docs/superpowers/plans/2026-08-22-chip-state-pr2-api.md). Same reasoning as
`test_setup_code.py`'s `Store` tests: this exercises a real `ON CONFLICT`
against 0020's `uq_chip_state_source_instance`, not a mocked store. Skipped
without `BELLASREEF_TEST_DATABASE_URL`, never pointed at the hub (see the
environment-boundary rule in CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from bellasreef_api.store import Store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

_needs_pg = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


async def _fresh_engine() -> AsyncEngine:
    """A `chip_state`-empty engine.

    Unlike `hub_identity`, `chip_state` has no singleton row to seed — the
    table just needs to be empty so each test's `(source, instance)` pairs
    start from nothing.
    """
    engine = create_async_engine(os.environ[_PG], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE chip_state"))
    return engine


@_needs_pg
def test_upsert_twice_keeps_one_row_and_second_write_wins() -> None:
    """Re-announcing the same chip updates in place (0020's unique
    constraint is the upsert target), and the row reflects the latest
    announcement, not the first."""

    async def scenario() -> list[dict[str, Any]]:
        engine = await _fresh_engine()
        store = Store(engine)

        await store.upsert_chip_state(
            source="pca9685",
            instance="0x40",
            initialised=False,
            initialised_at=None,
            facts={"pre_scale": 12},
            announced_at=datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
        )
        await store.upsert_chip_state(
            source="pca9685",
            instance="0x40",
            initialised=True,
            initialised_at=datetime(2026, 8, 22, 12, 0, 5, tzinfo=UTC),
            facts={"pre_scale": 12, "frequency_hz": 502.7},
            announced_at=datetime(2026, 8, 22, 12, 5, 0, tzinfo=UTC),
        )

        rows = await store.list_chip_state()
        await engine.dispose()
        return rows

    rows = asyncio.run(scenario())
    assert len(rows) == 1, "second announcement must update the existing row, not add one"
    row = rows[0]
    assert row["source"] == "pca9685"
    assert row["instance"] == "0x40"
    assert row["initialised"] is True
    assert row["initialised_at"] == datetime(2026, 8, 22, 12, 0, 5, tzinfo=UTC)
    assert row["facts"] == {"pre_scale": 12, "frequency_hz": 502.7}
    assert row["announced_at"] == datetime(2026, 8, 22, 12, 5, 0, tzinfo=UTC)


@_needs_pg
def test_list_orders_by_source_then_instance() -> None:
    """`list_chip_state` sorts, regardless of insert order — the API is the
    ordering authority, same reasoning as `list_capabilities`."""

    async def scenario() -> list[tuple[str, str]]:
        engine = await _fresh_engine()
        store = Store(engine)

        for source, instance in [
            ("wifi", "b"),
            ("pca9685", "1"),
            ("pca9685", "0"),
            ("wifi", "a"),
        ]:
            await store.upsert_chip_state(
                source=source,
                instance=instance,
                initialised=True,
                initialised_at=datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
                facts={},
                announced_at=datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
            )

        rows = await store.list_chip_state()
        await engine.dispose()
        return [(row["source"], row["instance"]) for row in rows]

    order = asyncio.run(scenario())
    assert order == [("pca9685", "0"), ("pca9685", "1"), ("wifi", "a"), ("wifi", "b")]


@_needs_pg
def test_facts_round_trip_mixed_value_types() -> None:
    """A facts dict with str/int/float/bool values survives the JSONB
    round trip with types intact, not coerced (e.g. a bool must not come
    back as an int)."""

    facts = {
        "chip": "pca9685",
        "pre_scale": 12,
        "frequency_hz": 502.7,
        "invrt": False,
        "initialised_by_probe": True,
    }

    async def scenario() -> dict[str, Any]:
        engine = await _fresh_engine()
        store = Store(engine)

        await store.upsert_chip_state(
            source="pca9685",
            instance="0x40",
            initialised=True,
            initialised_at=datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
            facts=facts,
            announced_at=datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
        )

        rows = await store.list_chip_state()
        await engine.dispose()
        return dict(rows[0]["facts"])

    round_tripped = asyncio.run(scenario())
    assert round_tripped == facts
    assert isinstance(round_tripped["chip"], str)
    assert isinstance(round_tripped["pre_scale"], int)
    assert isinstance(round_tripped["frequency_hz"], float)
    assert round_tripped["invrt"] is False
    assert round_tripped["initialised_by_probe"] is True

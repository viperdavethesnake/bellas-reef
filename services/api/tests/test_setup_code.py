# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Setup-code primitives: alphabet, normalization, hashing, rotation.

Pure-logic layer only (spec Feature 1, "Semantics"). Endpoint wiring and the
setup-mode gate on `POST /pair` are a later task; this covers what
`security.py` and `Store` need to expose for it.

The `Store` tests below need a real Postgres — `hub_identity` and its CHECK
constraints are the thing being tested, same reasoning as
`test_auth_lifecycle.py`. Skipped without `BELLASREEF_TEST_DATABASE_URL`,
never pointed at the hub (see the environment-boundary rule in CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

import pytest
from bellasreef_api.security import (
    SETUP_ALPHABET,
    format_setup_code,
    hash_setup_code,
    new_setup_code,
    normalize_setup_code,
)
from bellasreef_api.store import Store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"


def test_alphabet_has_no_confusables() -> None:
    assert set("0O1I").isdisjoint(SETUP_ALPHABET)
    # Spec: "8 characters from a confusable-free alphabet (Crockford base32
    # minus 0/O/1/I)". Crockford's own 32-symbol alphabet already excludes
    # I, L, O, U, so "minus 0/O/1/I" on top of that only removes 0 and 1 (O
    # and I are already absent) — 32 - 2 = 30, not 28. Implemented per the
    # spec's literal words (see SETUP_ALPHABET's construction in
    # security.py), and asserted here against what that construction
    # actually produces.
    assert len(SETUP_ALPHABET) == 30


def test_code_shape() -> None:
    code = new_setup_code()
    assert len(code) == 8 and set(code) <= set(SETUP_ALPHABET)
    assert format_setup_code(code) == f"{code[:4]}-{code[4:]}"


def test_entry_is_case_and_dash_insensitive() -> None:
    assert normalize_setup_code("7kf2-9qmd") == "7KF29QMD"
    assert hash_setup_code("7kf2-9qmd") == hash_setup_code("7KF29QMD")


def test_codes_are_not_reused() -> None:
    assert new_setup_code() != new_setup_code()  # 40 bits; collision = bug in randomness


# ------------------------------------------------------------------ Store
#
# `pytestmark` is deliberately not used here — it applies to the whole
# module, and the alphabet/normalization/hashing tests above need no
# database. Each Store test below carries its own skipif instead.

_needs_pg = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


async def _fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE hub_identity SET setup_code_hash = NULL, setup_completed_at = NULL")
        )
    return engine


@_needs_pg
def test_setup_state_starts_empty_and_rotation_overwrites() -> None:
    """`set_setup_code_hash` rotates: minting a new code invalidates the old
    one because only the latest hash is ever stored (spec: "exactly one
    code is valid at a time; minting a new one invalidates the old")."""

    async def scenario() -> dict[str, Any]:
        engine = await _fresh_engine()
        store = Store(engine)
        out: dict[str, Any] = {}

        out["initial"] = await store.setup_state()

        await store.set_setup_code_hash(hash_setup_code("AAAA-AAAA"))
        out["after_first_hash"] = (await store.setup_state())[0]

        await store.set_setup_code_hash(hash_setup_code("BBBB-BBBB"))
        out["after_second_hash"] = (await store.setup_state())[0]

        await engine.dispose()
        return out

    out = asyncio.run(scenario())
    assert out["initial"] == (None, None)
    assert out["after_first_hash"] == hash_setup_code("AAAA-AAAA")
    assert out["after_second_hash"] == hash_setup_code("BBBB-BBBB")
    assert out["after_first_hash"] != out["after_second_hash"], (
        "rotation must overwrite, not accumulate"
    )


@_needs_pg
def test_complete_setup_is_never_unset() -> None:
    """First successful pair stamps `setup_completed_at` and clears the
    setup-code hash; a second call must not move the timestamp (spec:
    "never unset" — revoking every client later must not re-enter setup
    mode)."""

    async def scenario() -> dict[str, Any]:
        engine = await _fresh_engine()
        store = Store(engine)
        out: dict[str, Any] = {}

        await store.set_setup_code_hash(hash_setup_code("AAAA-AAAA"))
        await store.complete_setup()
        first_hash, first_completed = await store.setup_state()
        out["hash_after_complete"] = first_hash
        out["completed_at_is_set"] = first_completed is not None

        # Move the stamped time back manually, then confirm a second call
        # leaves it alone rather than bumping it to "now" again.
        assert first_completed is not None
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE hub_identity SET setup_completed_at = :past"),
                {"past": first_completed - timedelta(days=1)},
            )
        moved_completed = (await store.setup_state())[1]
        await store.complete_setup()
        out["unchanged_by_second_call"] = (await store.setup_state())[1] == moved_completed

        await engine.dispose()
        return out

    out = asyncio.run(scenario())
    assert out["hash_after_complete"] is None, "completion rotates out any live setup code"
    assert out["completed_at_is_set"] is True
    assert out["unchanged_by_second_call"] is True

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Backup and restore against a real Postgres (PRD R14).

The question a backup has to answer is not "did a file appear" — it is "can the
tank be run from this six months from now, on hardware that has never seen it".
So the assertion here is behavioural: restore into a database that did not
exist a moment ago, then have a client that paired with the *old* hub present
its refresh token to the *new* one and get a working access token back.

That path is only green if two separate things survived the round trip — the
``paired_clients`` row and the signing key. The second is the one worth being
careful about: ``Store.signing_secret()`` *generates and stores a key* when it
finds none, so a restore that dropped ``signing_keys`` entirely would still
mint a verifiable token and this test would pass while every phone in the house
was logged out. That is why the restored secret is compared against the
original rather than merely used.

The negative direction is the other half. A damaged archive has to fail with a
name, and it has to fail with the target database exactly as untouched as it
was before — a half-restored hub is worse than no restore, because it looks
like one.

Needs `BELLASREEF_TEST_DATABASE_URL` and the PostgreSQL client tools. Every
durable it creates — two scratch databases, a VictoriaMetrics snapshot, a
seeded client row — is removed on teardown.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from bellasreef_api.app import CONTRACTS_VERSION
from bellasreef_api.backup import (
    PG_BIN_ENV,
    RestoreRefusedError,
    create_backup,
    pg_tools_available,
    restore_backup,
)
from bellasreef_api.security import issue_access_token, verify_access_token
from bellasreef_api.store import Store
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"
_VM = "BELLASREEF_TEST_VM_URL"

pytestmark = [
    pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set"),
    pytest.mark.skipif(
        not pg_tools_available(),
        reason=(
            f"pg_dump/pg_restore not found on PATH and {PG_BIN_ENV} not set; "
            "backup shells out to the real PostgreSQL client tools"
        ),
    ),
]


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


# ------------------------------------------------------------ scratch database


async def _create_scratch_database() -> str:
    """A brand-new, empty database. Returns its DSN.

    Created through the ``postgres`` maintenance database because CREATE
    DATABASE cannot run inside a transaction — hence AUTOCOMMIT.
    """
    base = make_url(os.environ[_PG])
    name = f"bellasreef_restore_{uuid.uuid4().hex[:12]}"

    admin = create_async_engine(
        base.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    return base.set(database=name).render_as_string(hide_password=False)


async def _drop_scratch_database(dsn: str) -> None:
    base = make_url(dsn)
    name = base.database
    admin = create_async_engine(
        base.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as conn:
            # FORCE terminates any connection still attached; without it a
            # leaked engine turns cleanup into a hang rather than a failure.
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _table_count(dsn: str) -> int:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


@asynccontextmanager
async def _scratch() -> AsyncIterator[str]:
    """An empty database for the duration of the block, dropped on the way out."""
    dsn = await _create_scratch_database()
    try:
        yield dsn
    finally:
        await _drop_scratch_database(dsn)


# ------------------------------------------------------------------- seeding


async def _seed_client(name: str) -> tuple[str, str, str]:
    """Put a paired client and a signing key in the source database.

    Returns ``(client_id, refresh_token, signing_secret)``.
    """
    engine = create_async_engine(os.environ[_PG])
    try:
        store = Store(engine)
        secret = await store.signing_secret()
        client_id, refresh = await store.create_client(name)
        return str(client_id), refresh, secret
    finally:
        await engine.dispose()


async def _forget_client(client_id: str) -> None:
    engine = create_async_engine(os.environ[_PG])
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM paired_clients WHERE id = :id"), {"id": client_id})
    finally:
        await engine.dispose()


async def _delete_vm_snapshot(vm_url: str, snapshot: str) -> None:
    """Snapshots are durable state in the VM volume. Tests do not leave them."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        await http.post(f"{vm_url}/snapshot/delete", params={"snapshot": snapshot})


async def _backup_to(archive: Path) -> Any:
    return await create_backup(
        dsn=os.environ[_PG],
        out=archive,
        vm_url=os.environ.get(_VM),
        tool_version="test",
        contracts_version=CONTRACTS_VERSION,
    )


# ------------------------------------------------------------------ the round trip


def test_a_client_paired_with_the_old_hub_mints_a_token_on_the_restored_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client_id, refresh, original_secret = await _seed_client("phone-under-test")
        result = await _backup_to(tmp_path / "backup.tar.gz")

        try:
            async with _scratch() as dsn:
                await restore_backup(dsn=dsn, archive=result.archive, workdir=tmp_path / "work")

                engine = create_async_engine(dsn)
                try:
                    restored = Store(engine)

                    # The phone still holds only its refresh token.
                    resolved = await restored.client_for_refresh_token(refresh)
                    assert resolved is not None, (
                        "the restored hub does not recognise a client that paired with the "
                        "hub the backup was taken from"
                    )
                    assert str(resolved) == client_id

                    secret = await restored.signing_secret()
                    assert secret == original_secret, (
                        "the restored hub minted a NEW signing key, which means signing_keys "
                        "did not survive the restore — every existing session is dead even "
                        "though this token verifies"
                    )

                    token, _ = issue_access_token(resolved, secret)
                    assert str(verify_access_token(token, secret)) == client_id
                finally:
                    await engine.dispose()
        finally:
            await _forget_client(client_id)
            snapshot = result.manifest.telemetry.snapshot
            if snapshot and (vm := os.environ.get(_VM)):
                await _delete_vm_snapshot(vm, snapshot)

    run(scenario)


def test_the_manifest_records_the_hub_it_came_from_and_what_it_left_behind(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        result = await _backup_to(tmp_path / "backup.tar.gz")
        manifest = result.manifest
        try:
            engine = create_async_engine(os.environ[_PG])
            try:
                async with engine.connect() as conn:
                    stamped = (
                        await conn.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalar_one()
            finally:
                await engine.dispose()

            assert manifest.schema_revision == stamped
            assert manifest.contracts_version == CONTRACTS_VERSION
            assert manifest.hub.database == make_url(os.environ[_PG]).database
            assert manifest.hub.postgres_version
            assert manifest.hub.taken_on

            # The point of the omissions list: absence has to be legible.
            assert manifest.omissions, "a backup that lists no omissions is claiming to be total"
            described = " ".join(o.what.lower() for o in manifest.omissions)
            assert "telemetry" in described
            assert "jetstream" in described or "nats" in described
            for omission in manifest.omissions:
                assert omission.why and omission.recover, (
                    f"omission {omission.what!r} says what is missing but not how to get it"
                )
        finally:
            snapshot = manifest.telemetry.snapshot
            if snapshot and (vm := os.environ.get(_VM)):
                await _delete_vm_snapshot(vm, snapshot)

    run(scenario)


@pytest.mark.skipif(not os.environ.get(_VM), reason=f"{_VM} not set")
def test_the_telemetry_snapshot_is_really_taken(tmp_path: Path) -> None:
    """`/snapshot/create` against a real VictoriaMetrics, not a recorded name."""

    async def scenario() -> None:
        result = await _backup_to(tmp_path / "backup.tar.gz")
        telemetry = result.manifest.telemetry
        vm = os.environ[_VM]
        try:
            assert telemetry.taken is True
            assert telemetry.snapshot
            assert telemetry.vm_url == vm

            async with httpx.AsyncClient(timeout=10.0) as http:
                listing = await http.get(f"{vm}/snapshot/list")
            assert telemetry.snapshot in listing.json()["snapshots"]
        finally:
            if telemetry.snapshot:
                await _delete_vm_snapshot(vm, telemetry.snapshot)

    run(scenario)


# ------------------------------------------------------- damaged archives


def test_a_truncated_archive_is_refused_and_the_target_is_untouched(tmp_path: Path) -> None:
    async def scenario() -> None:
        result = await _backup_to(tmp_path / "backup.tar.gz")
        whole = result.archive.read_bytes()
        result.archive.write_bytes(whole[: len(whole) // 2])

        try:
            async with _scratch() as dsn:
                assert await _table_count(dsn) == 0

                with pytest.raises(RestoreRefusedError) as caught:
                    await restore_backup(dsn=dsn, archive=result.archive, workdir=tmp_path / "work")
                assert caught.value.reason == "archive-unreadable"

                assert await _table_count(dsn) == 0, (
                    "a refused restore left tables behind — this is the silent partial "
                    "restore the whole ordering rule exists to prevent"
                )
        finally:
            snapshot = result.manifest.telemetry.snapshot
            if snapshot and (vm := os.environ.get(_VM)):
                await _delete_vm_snapshot(vm, snapshot)

    run(scenario)


def test_a_corrupted_dump_is_refused_by_digest_and_the_target_is_untouched(
    tmp_path: Path,
) -> None:
    """The tar is intact and the manifest parses. Only the digest disagrees."""

    async def scenario() -> None:
        result = await _backup_to(tmp_path / "backup.tar.gz")

        # Rewrite one byte deep inside the gzip stream: the archive still opens,
        # the manifest still reads, and the dump comes out wrong.
        raw = bytearray(result.archive.read_bytes())
        midpoint = len(raw) * 3 // 4
        raw[midpoint] ^= 0xFF
        result.archive.write_bytes(bytes(raw))

        try:
            async with _scratch() as dsn:
                with pytest.raises(RestoreRefusedError) as caught:
                    await restore_backup(dsn=dsn, archive=result.archive, workdir=tmp_path / "work")
                # Either the gzip checksum notices or our digest does. Both are
                # loud, both are named, and neither writes to the database.
                assert caught.value.reason in {"payload-corrupt", "archive-unreadable"}

                assert await _table_count(dsn) == 0
        finally:
            snapshot = result.manifest.telemetry.snapshot
            if snapshot and (vm := os.environ.get(_VM)):
                await _delete_vm_snapshot(vm, snapshot)

    run(scenario)


def test_restoring_over_a_populated_database_is_refused_by_default(tmp_path: Path) -> None:
    """Fresh hardware means an empty database. Anything else needs saying so."""

    async def scenario() -> None:
        result = await _backup_to(tmp_path / "backup.tar.gz")

        try:
            async with _scratch() as dsn:
                await restore_backup(dsn=dsn, archive=result.archive, workdir=tmp_path / "work")
                before = await _table_count(dsn)
                assert before > 0

                with pytest.raises(RestoreRefusedError) as caught:
                    await restore_backup(
                        dsn=dsn, archive=result.archive, workdir=tmp_path / "work2"
                    )
                assert caught.value.reason == "target-not-empty"
                assert await _table_count(dsn) == before
        finally:
            snapshot = result.manifest.telemetry.snapshot
            if snapshot and (vm := os.environ.get(_VM)):
                await _delete_vm_snapshot(vm, snapshot)

    run(scenario)

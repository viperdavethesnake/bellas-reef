# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""`bellasreef` — the operator CLI.

Two jobs, both of them things you reach for on a bad day.

**pair** is the fire escape (auth.md §1), not the front door. It exists for one
situation: every paired client is lost or revoked, so there is nobody left to
approve a new one and the TOFU-ever window is shut by design.

It opens a **bounded pairing window** rather than clearing client state. That
distinction is the whole point — deleting revoked clients would reopen the
TOFU-ever window, which is keyed on rows having existed precisely so that
revoking everything cannot reopen open pairing. A recovery path that undid the
protection it is recovering from would be a much better attack than a feature.

**backup**/**restore** are PRD R14. One command produces one restorable file;
one command loads it onto fresh hardware or refuses, by name, without touching
the target. See :mod:`bellasreef_api.backup` for why the refusal ordering is
the way it is, and ``docs/backup-restore.md`` for the operator flow.

This is the only terminal interaction in the system. Reaching for it should feel
like reaching for a fire escape.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import socket
import sys
import tempfile
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

from bellasreef_api.app import CONTRACTS_VERSION
from bellasreef_api.backup import (
    BackupError,
    RestoreRefusedError,
    create_backup,
    restore_backup,
)
from bellasreef_api.store import Store

__all__ = ["main"]

#: auth.md §1: five minutes. Long enough to pick up a phone and open the app,
#: short enough that walking away does not leave the door open.
DEFAULT_WINDOW_S = 300


async def _open_window(dsn: str, ttl_s: float, nats_url: str | None) -> dict[str, Any]:
    engine = create_async_engine(dsn, future=True)
    store = Store(engine)
    try:
        opened_by = f"{getpass.getuser()}@{socket.gethostname()}"
        window_id, expires = await store.open_pairing_window(opened_by, ttl_s)
        total = await store.total_clients_ever()
        active = await store.active_client_count()
    finally:
        await engine.dispose()

    if nats_url:
        # Best effort: the window is already open, and failing to announce it
        # must not stop the operator recovering their tank.
        from bellasreef_api.audit import NatsAuditSink

        sink = NatsAuditSink(nats_url, source="bellasreef-cli")
        await sink(
            "pair.window_opened",
            {
                "window_id": str(window_id),
                "opened_by": opened_by,
                "expires_at": expires.isoformat(),
                "ttl_s": ttl_s,
            },
        )
        await sink.close()

    return {
        "window_id": str(window_id),
        "expires_at": expires.isoformat(),
        "expires_in_s": round((expires - datetime.now(UTC)).total_seconds()),
        "opened_by": opened_by,
        "clients_ever": total,
        "clients_active": active,
    }


def _tool_version() -> str:
    try:
        return version("bellasreef-api")
    except PackageNotFoundError:
        return "unknown"


def _backup_command(args: Any, dsn: str) -> int:
    vm_url = args.vm_url or os.environ.get("BELLASREEF_VM_URL")
    if vm_url and args.no_telemetry_snapshot:
        print(
            "--no-telemetry-snapshot conflicts with a VictoriaMetrics URL being available",
            file=sys.stderr,
        )
        return 2
    if not vm_url and not args.no_telemetry_snapshot:
        # Never inferred from an unset variable. A hub whose telemetry silently
        # stopped being captured is exactly the failure this whole command
        # exists to make impossible.
        print(
            "No VictoriaMetrics URL. Set BELLASREEF_VM_URL or pass --vm-url, or say\n"
            "--no-telemetry-snapshot to take a backup without one on purpose.",
            file=sys.stderr,
        )
        return 2

    out = args.out
    if out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path(f"bellasreef-{socket.gethostname()}-{stamp}.tar.gz")

    try:
        result = asyncio.run(
            create_backup(
                dsn=dsn,
                out=out,
                vm_url=vm_url,
                tool_version=_tool_version(),
                contracts_version=CONTRACTS_VERSION,
            )
        )
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 4

    manifest = result.manifest
    if args.json:
        print(manifest.model_dump_json(indent=2))
        return 0

    print(f"Wrote {result.archive} ({result.archive.stat().st_size:,} bytes)")
    print()
    print(f"  database        : {manifest.hub.database} on {manifest.hub.database_host}")
    print(f"  taken on        : {manifest.hub.taken_on}")
    print(f"  schema revision : {manifest.schema_revision}")
    print(f"  contracts       : {manifest.contracts_version}")
    print(f"  taken           : {manifest.created_at.isoformat()}")
    print()
    _print_omissions(manifest)
    return 0


def _print_omissions(manifest: Any) -> None:
    """Say what is NOT in the archive, every time, unprompted.

    Printed at both backup and restore. The operator who needs this sentence is
    the one who never read the docs, and the moment they will read it is the
    moment the command runs.
    """
    if manifest.telemetry.taken:
        print(f"  Telemetry snapshot: {manifest.telemetry.snapshot}")
    print("  NOT in this archive:")
    for omission in manifest.omissions:
        print(f"    - {omission.what}")
    print()
    print("  Run with --json to see why each one is absent and how to recover it.")


def _restore_command(args: Any, dsn: str) -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="bellasreef-restore-") as workdir:
            manifest = asyncio.run(
                restore_backup(
                    dsn=dsn,
                    archive=args.archive,
                    workdir=Path(workdir),
                    force=args.force,
                )
            )
    except RestoreRefusedError as exc:
        print(f"restore refused [{exc.reason}]", file=sys.stderr)
        print(f"  {exc.detail}", file=sys.stderr)
        print(file=sys.stderr)
        print("  The target database was not modified.", file=sys.stderr)
        return 3
    except BackupError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 4

    if args.json:
        print(manifest.model_dump_json(indent=2))
        return 0

    print(
        f"Restored from a backup of {manifest.hub.database} on "
        f"{manifest.hub.database_host}, taken on {manifest.hub.taken_on}"
    )
    print(f"  taken           : {manifest.created_at.isoformat()}")
    print(f"  schema revision : {manifest.schema_revision}")
    print()
    _print_omissions(manifest)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bellasreef",
        description="Bella's Reef hub administration. Recovery only.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser(
        "pair",
        help="open a temporary pairing window",
        description=(
            "Open a bounded window during which one new client may pair without "
            "approval from an existing one. For use when every paired client has "
            "been lost or revoked."
        ),
    )
    pair.add_argument(
        "--ttl",
        type=float,
        default=DEFAULT_WINDOW_S,
        metavar="SECONDS",
        help=f"how long the window stays open (default {DEFAULT_WINDOW_S})",
    )
    pair.add_argument("--json", action="store_true", help="machine-readable output")

    backup = sub.add_parser(
        "backup",
        help="write a restorable archive of this hub",
        description=(
            "Dump PostgreSQL and record what the dump is, where it came from, and "
            "what it deliberately does not contain."
        ),
    )
    backup.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help="archive path (default: ./bellasreef-<host>-<timestamp>.tar.gz)",
    )
    backup.add_argument(
        "--vm-url",
        metavar="URL",
        help="VictoriaMetrics base URL (default: $BELLASREEF_VM_URL)",
    )
    backup.add_argument(
        "--no-telemetry-snapshot",
        action="store_true",
        help=(
            "proceed without a VictoriaMetrics snapshot. Required if no VM URL is "
            "available, so that a backup missing telemetry is always a decision"
        ),
    )
    backup.add_argument("--json", action="store_true", help="machine-readable output")

    restore = sub.add_parser(
        "restore",
        help="load an archive into an empty database",
        description=(
            "Verify an archive completely, then load it. Any problem is reported by "
            "name and the target database is left untouched."
        ),
    )
    restore.add_argument("archive", type=Path, help="path to a backup archive")
    restore.add_argument(
        "--force",
        action="store_true",
        help="drop and replace objects already in the target (default: refuse)",
    )
    restore.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args(argv)

    dsn = os.environ.get("BELLASREEF_DATABASE_URL")
    if not dsn:
        print("BELLASREEF_DATABASE_URL is not set", file=sys.stderr)
        return 2

    if args.command == "backup":
        return _backup_command(args, dsn)
    if args.command == "restore":
        return _restore_command(args, dsn)

    if args.ttl <= 0:
        print("--ttl must be greater than zero", file=sys.stderr)
        return 2

    result = asyncio.run(_open_window(dsn, args.ttl, os.environ.get("BELLASREEF_NATS_URL")))

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"Pairing window open for {result['expires_in_s']}s.")
    print()
    print("  Open the app and pair now. The window is spent by the first client")
    print("  that uses it, or expires on its own.")
    print()
    print(f"  clients ever paired : {result['clients_ever']}")
    print(f"  clients still live  : {result['clients_active']}")
    print(f"  opened by           : {result['opened_by']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

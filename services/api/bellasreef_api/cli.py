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
import contextlib
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


def _devices_import(args: Any) -> int:
    """Bind everything a device file declares, through the API.

    Explicitly **not** the product mechanism. The registry is: hardware
    announces its capabilities, an operator binds them, and the app is the
    surface for that. This exists for seeding a bench hub and for restoring one
    from notes, and it earns its place only by going through the same endpoint
    with the same validation — including the rule that matching hardware is
    adopted rather than duplicated.

    A file that binds nothing is reported as such rather than as success. "0
    devices imported" and "imported" look the same in a terminal at 1am.
    """
    import yaml

    token = args.token or os.environ.get("BELLASREEF_TOKEN")
    if not token:
        print(
            "no token. Pass --token or set BELLASREEF_TOKEN — this writes through "
            "the API like any other client, so it needs a credential like one.",
            file=sys.stderr,
        )
        return 2

    try:
        raw = yaml.safe_load(args.file.read_text())
    except (OSError, yaml.YAMLError) as exc:
        print(f"could not read {args.file}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(raw, dict):
        print(f"{args.file} must be a mapping at the top level", file=sys.stderr)
        return 2

    requests: list[dict[str, Any]] = []
    for entry in raw.get("actuators") or []:
        binding = entry.get("binding") or {}
        requests.append(
            {
                "device_id": entry.get("id"),
                "driver_type": binding.get("driver"),
                "channel": str(binding.get("channel")),
                "role": entry.get("role", "light"),
                "display_name": entry.get("display_name"),
                "location": entry.get("location"),
            }
        )
    for entry in raw.get("sensors") or []:
        binding = entry.get("binding") or {}
        requests.append(
            {
                "device_id": entry.get("id"),
                "driver_type": binding.get("driver"),
                "channel": binding.get("rom"),
                "display_name": entry.get("display_name"),
                "location": entry.get("location"),
                "poll_interval_s": binding.get("poll_interval_s", 5.0),
            }
        )

    if not requests:
        print(f"{args.file} declares no devices", file=sys.stderr)
        return 2

    results = asyncio.run(_post_bindings(args.api, token, requests))
    failures = [r for r in results if r["status"] >= 400]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            if result["status"] >= 400:
                print(f"  FAIL  {result['device_id']}: {result['detail']}")
            else:
                verb = "created" if result.get("created") else "bound existing"
                print(f"  ok    {result['device_id']} -> {verb}")
        print()
        print(f"{len(results) - len(failures)}/{len(results)} bound")

    return 1 if failures else 0


async def _post_bindings(
    api: str, token: str, requests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    import httpx

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=15.0) as http:
        for request in requests:
            payload = {k: v for k, v in request.items() if v is not None}
            try:
                response = await http.post(
                    f"{api}/api/v1/devices",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                results.append(
                    {"device_id": request["device_id"], "status": 599, "detail": str(exc)}
                )
                continue
            body: dict[str, Any] = {}
            with contextlib.suppress(ValueError):
                body = response.json()
            results.append(
                {
                    "device_id": request["device_id"],
                    "status": response.status_code,
                    "created": body.get("created"),
                    "detail": body.get("detail", response.text[:200]),
                }
            )
    return results


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

    devices = sub.add_parser(
        "devices",
        help="device registry conveniences",
        description=(
            "Seeding and restore aid. The registry is the product mechanism; "
            "this writes through the same API a client would."
        ),
    )
    device_sub = devices.add_subparsers(dest="device_command", required=True)

    imp = device_sub.add_parser(
        "import",
        help="bind devices declared in a YAML file",
        description=(
            "Reads a devices.yaml and binds each entry through POST "
            "/api/v1/devices. Convenience only: identical to doing it from the "
            "app, and subject to the same validation — including matching "
            "existing hardware rather than creating beside it."
        ),
    )
    imp.add_argument("file", type=Path, help="a devices.yaml")
    imp.add_argument("--api", default="http://localhost:8000", help="hub API base URL")
    imp.add_argument("--token", help="access token (default: $BELLASREEF_TOKEN)")
    imp.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args(argv)

    dsn = os.environ.get("BELLASREEF_DATABASE_URL")
    if not dsn:
        print("BELLASREEF_DATABASE_URL is not set", file=sys.stderr)
        return 2

    if args.command == "devices":
        return _devices_import(args)

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

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""`bellasreef` — the operator CLI.

A handful of jobs, all of them things you reach for on a bad day.

**pair** is the fire escape (auth.md §1), not the front door. It exists for one
situation: every paired client is lost or revoked, so there is nobody left to
approve a new one and the TOFU-ever window is shut by design.

It opens a **bounded pairing window** rather than clearing client state. That
distinction is the whole point — deleting revoked clients would reopen the
TOFU-ever window, which is keyed on rows having existed precisely so that
revoking everything cannot reopen open pairing. A recovery path that undid the
protection it is recovering from would be a much better attack than a feature.

**revoke** is the other half of the same recovery. A window *adds* a client; it
never removes one, deliberately, so pairing a replacement phone leaves the lost
one paired beside it. Both revoke endpoints need a live token of your own, which
is precisely what an operator whose only device is gone does not have. So:
`bellasreef pair` to let the new phone in, `bellasreef revoke` to turn the old
one off, and those two commands together are how you replace a phone.

It also exists to retire the last reason anyone would reach for `psql`. The
audit row is written by the handler, not by the mutation, so a revocation done
in SQL leaves no trace at all — which is how one went missing on 2026-08-12.
This subcommand emits `client.revoked` exactly as the API does, and says so out
loud when it cannot.

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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from bellasreef_api.app import CONTRACTS_VERSION
from bellasreef_api.backup import (
    BackupError,
    RestoreRefusedError,
    create_backup,
    restore_backup,
)
from bellasreef_api.security import format_setup_code, hash_setup_code, new_setup_code
from bellasreef_api.store import ClientRow, Store

__all__ = ["main"]

#: auth.md §1: five minutes. Long enough to pick up a phone and open the app,
#: short enough that walking away does not leave the door open.
DEFAULT_WINDOW_S = 300


async def _emit(
    nats_url: str | None, event: str, detail: dict[str, Any], category: str = "auth"
) -> str | None:
    """Publish one audit event. Returns a warning to shout, or ``None``.

    Best effort by design — the window is already open, the client is already
    revoked, and a broker that is down must not stop an operator recovering
    their tank. But best effort **said out loud**. This used to be a bare
    ``if nats_url:``, so the one scenario the CLI exists for — locked out, at
    the hub, over SSH, with `BELLASREEF_NATS_URL` living in a systemd env file
    the shell has never heard of — printed the same success banner and exit 0 as
    a fully recorded run. The audit gap was invisible at exactly the moment
    somebody was standing there to see it.

    A CRITICAL log line is not a substitute. Nobody is tailing the journal while
    they type this.
    """
    if not nats_url:
        return (
            f"BELLASREEF_NATS_URL is not set, so `{event}` was NOT recorded.\n"
            "  The operation itself succeeded; the audit trail has a hole where\n"
            "  it should be. The value is in /etc/bellasreef/api.env on the hub."
        )

    from bellasreef_api.audit import NatsAuditSink

    sink = NatsAuditSink(nats_url, source="bellasreef-cli")
    await sink(event, detail, category=category)
    await sink.close()
    if sink.failures:
        return (
            f"the audit sink at {nats_url} could not be reached, so `{event}`\n"
            "  was NOT recorded. The operation itself succeeded."
        )
    return None


def _warn(message: str) -> None:
    """One shape for every "it worked, but" on this CLI. stderr, and loud."""
    print(file=sys.stderr)
    print(f"  !! {message}", file=sys.stderr)
    print(file=sys.stderr)


async def _open_window(
    dsn: str, ttl_s: float, nats_url: str | None
) -> tuple[dict[str, Any], str | None]:
    engine = create_async_engine(dsn, future=True)
    store = Store(engine)
    try:
        opened_by = f"{getpass.getuser()}@{socket.gethostname()}"
        window_id, expires = await store.open_pairing_window(opened_by, ttl_s)
        total = await store.total_clients_ever()
        active = await store.active_client_count()
    finally:
        await engine.dispose()

    warning = await _emit(
        nats_url,
        "pair.window_opened",
        {
            "window_id": str(window_id),
            "opened_by": opened_by,
            "expires_at": expires.isoformat(),
            "ttl_s": ttl_s,
        },
    )

    return {
        "window_id": str(window_id),
        "expires_at": expires.isoformat(),
        "expires_in_s": round((expires - datetime.now(UTC)).total_seconds()),
        "opened_by": opened_by,
        "clients_ever": total,
        "clients_active": active,
    }, warning


# ------------------------------------------------------------------- revoke


def _stamp(when: datetime | None) -> str:
    return "never" if when is None else when.isoformat(timespec="seconds")


def _client_json(row: ClientRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "created_at": row.created_at.isoformat(),
        "last_seen_at": None if row.last_seen_at is None else row.last_seen_at.isoformat(),
        "revoked_at": None if row.revoked_at is None else row.revoked_at.isoformat(),
        "active": row.revoked_at is None,
    }


def _print_clients(rows: Sequence[ClientRow]) -> None:
    """Two lines per client, full id first.

    Not a column table. The id is a 36-character UUID and it is the thing the
    operator has to type back, so it gets a line of its own rather than being
    truncated to fit beside four timestamps on an 80-column SSH session.
    """
    if not rows:
        print("No client has ever paired with this hub.")
        return

    live = sum(1 for row in rows if row.revoked_at is None)
    print(f"{len(rows)} client(s) ever paired, {live} still live.")
    print()
    for row in rows:
        state = "live" if row.revoked_at is None else f"REVOKED {_stamp(row.revoked_at)}"
        print(f"  {row.id}  {row.name}")
        print(
            f"      paired {_stamp(row.created_at)} · last seen "
            f"{_stamp(row.last_seen_at)} · {state}"
        )
    print()
    print("  Revoke one with:  bellasreef revoke <id|name>")


def _resolve(rows: Sequence[ClientRow], target: str) -> tuple[ClientRow | None, list[ClientRow]]:
    """Find the client ``target`` names. Returns ``(match, ambiguous_candidates)``.

    An id is exact and final: if it does not match a row, nothing does, and
    falling through to a name search would let a typo'd UUID revoke a device
    that happens to be called something similar.

    Names are matched exactly first, then case-insensitively, and **two matches
    are never resolved by picking one**. Client names come from whatever the app
    sent; two phones called "iPhone" is the expected case, not the exotic one
    (see auth-review B9), and guessing between them revokes the wrong tank
    control at the moment somebody is already having a bad day.
    """
    try:
        wanted = UUID(target)
    except ValueError:
        pass
    else:
        for row in rows:
            if row.id == wanted:
                return row, []
        return None, []

    matches = [row for row in rows if row.name == target]
    if not matches:
        matches = [row for row in rows if row.name.casefold() == target.casefold()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


@dataclass(frozen=True, slots=True)
class _RevokeOutcome:
    #: "revoked" | "unknown" | "ambiguous" | "already-revoked" | "raced"
    status: str
    client: ClientRow | None = None
    candidates: tuple[ClientRow, ...] = ()
    audit_warning: str | None = None


async def _list_clients(dsn: str) -> list[ClientRow]:
    engine = create_async_engine(dsn, future=True)
    try:
        return await Store(engine).list_clients()
    finally:
        await engine.dispose()


async def _revoke(dsn: str, target: str, nats_url: str | None) -> _RevokeOutcome:
    """Resolve, revoke, and audit — in that order, in one connection.

    The audit event carries no ``revoked_by``. Every other producer of
    ``client.revoked`` puts a client UUID in that field, and there is no client
    here: the actor is a person at a terminal. Writing "david@bellasreef" into a
    field consumers read as an id would corrupt the type for the sake of looking
    complete. The sink stamps ``actor="bellasreef-cli"`` on every event it
    publishes, which is the honest answer to who did this, and ``operator``
    carries the shell detail beside it.
    """
    engine = create_async_engine(dsn, future=True)
    store = Store(engine)
    try:
        rows = await store.list_clients()
        match, candidates = _resolve(rows, target)
        if match is None:
            status = "ambiguous" if candidates else "unknown"
            return _RevokeOutcome(status, candidates=tuple(candidates))
        if match.revoked_at is not None:
            return _RevokeOutcome("already-revoked", client=match)
        if not await store.revoke(match.id):
            # Read live, revoked between the two statements. Reported rather
            # than smoothed over: the operator asked for a state change and did
            # not cause one.
            return _RevokeOutcome("raced", client=match)
    finally:
        await engine.dispose()

    warning = await _emit(
        nats_url,
        "client.revoked",
        {
            "client_id": str(match.id),
            "client_name": match.name,
            "revoked_via": "cli",
            "operator": f"{getpass.getuser()}@{socket.gethostname()}",
        },
    )
    return _RevokeOutcome("revoked", client=match, audit_warning=warning)


def _revoke_command(args: Any, dsn: str) -> int:
    nats_url = os.environ.get("BELLASREEF_NATS_URL")

    if args.list:
        rows = asyncio.run(_list_clients(dsn))
        if args.json:
            print(json.dumps([_client_json(row) for row in rows], indent=2))
        else:
            _print_clients(rows)
        return 0

    if args.client is None:
        print(
            "name a client to revoke — its id or its name — or pass --list to see\n"
            "what this hub knows about.",
            file=sys.stderr,
        )
        return 2

    outcome = asyncio.run(_revoke(dsn, args.client, nats_url))

    if args.json:
        print(
            json.dumps(
                {
                    "status": outcome.status,
                    "client": None if outcome.client is None else _client_json(outcome.client),
                    "candidates": [_client_json(row) for row in outcome.candidates],
                    "audit_warning": outcome.audit_warning,
                },
                indent=2,
            )
        )
    if outcome.status == "ambiguous":
        if not args.json:
            print(
                f"{args.client!r} names {len(outcome.candidates)} clients. Nothing was revoked.",
                file=sys.stderr,
            )
            print(file=sys.stderr)
            for row in outcome.candidates:
                state = "live" if row.revoked_at is None else "revoked"
                print(f"    {row.id}  {row.name}  ({state})", file=sys.stderr)
            print(file=sys.stderr)
            print("  Name one by id.", file=sys.stderr)
        return 2
    if outcome.status == "unknown":
        if not args.json:
            print(
                f"no client with id or name {args.client!r}. "
                "`bellasreef revoke --list` shows them all.",
                file=sys.stderr,
            )
        return 2
    if outcome.status == "already-revoked":
        assert outcome.client is not None
        if not args.json:
            print(
                f"{outcome.client.name} ({outcome.client.id}) was already revoked at "
                f"{_stamp(outcome.client.revoked_at)}. Nothing to do.",
                file=sys.stderr,
            )
        return 2
    if outcome.status == "raced":
        assert outcome.client is not None
        if not args.json:
            print(
                f"{outcome.client.name} ({outcome.client.id}) was revoked by somebody "
                "else between reading it and writing it. Nothing was done here.",
                file=sys.stderr,
            )
        return 2

    assert outcome.client is not None
    if outcome.audit_warning:
        _warn(outcome.audit_warning)
    if args.json:
        return 0

    print(f"Revoked {outcome.client.name} ({outcome.client.id}).")
    print()
    print("  Its refresh token is dead and any access token it holds stops working")
    print("  on the next request. The client row stays, revoked — that is what keeps")
    print("  the open-pairing window shut.")
    return 0


# --------------------------------------------------------------- setup-code


async def _setup_code(dsn: str) -> str | None:
    """Mint a new setup code, or report that there is nothing to mint.

    Returns the freshly minted (unformatted) code, or ``None`` when setup is
    already complete. ``hub_id()`` runs first to seed ``hub_identity`` if this
    is the first thing to ever touch that row — ``setup_state`` and
    ``set_setup_code_hash`` are both plain ``UPDATE``s that no-op on a missing
    row rather than erroring, so skipping this would mint a code, "store" its
    hash into nothing, and print a code that verifies against no hash at all.
    """
    engine = create_async_engine(dsn, future=True)
    try:
        store = Store(engine)
        await store.hub_id()
        _, completed_at = await store.setup_state()
        if completed_at is not None:
            return None
        code = new_setup_code()
        await store.set_setup_code_hash(hash_setup_code(code))
        return code
    finally:
        await engine.dispose()


def _setup_code_command(args: Any, dsn: str) -> int:
    code = asyncio.run(_setup_code(dsn))
    if code is None:
        print(
            "Setup is complete. Pair new devices from the approver "
            "screen on an already-paired device, or open a window with "
            "`bellasreef pair` as the fire-escape."
        )
        return 0

    print(f"Setup code: {format_setup_code(code)}")
    print("Open the Bella's Reef app on this network and enter this code when asked.")
    return 0


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
    _print_contains(manifest)
    _print_omissions(manifest)
    return 0


def _print_contains(manifest: Any) -> None:
    """Say what IS in the archive, before saying what is not.

    The omissions list has been printed since the first backup because absence
    is invisible. Presence turned out to be invisible in the other direction: an
    operator who reads a careful enumeration of what is missing concludes that
    what is present is the boring part, and puts the file on a USB stick. The
    archive carries the JWT signing secret in plaintext and there is no key
    rotation, so that copy is a permanent credential for this hub.

    It reached ``manifest.json`` and the docs and stopped there, which is the
    one place nobody was looking — the terminal is where the file has just been
    written and where the operator is deciding what to do with it.

    Printed first, and as a warning rather than a bullet, because it is the more
    important of the two lists.
    """
    if not getattr(manifest, "contains", None):
        return
    print("  !! WARNING — this archive is itself a credential:")
    for item in manifest.contains:
        print(f"       {item.what}")
        print(f"       {item.handling}")
    print()


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
    _print_contains(manifest)
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

    sub.add_parser(
        "setup-code",
        help="mint the first-pair setup code (setup mode only)",
        description=(
            "In setup mode: mint a new setup code, rotating out any previous one, "
            "for the new-owner adoption flow — no SSH pairing window required. "
            "After the first pair, informational only: setup mode is closed and "
            "`bellasreef pair` is the fire escape from then on."
        ),
    )

    # A mode of `revoke` rather than a sibling `bellasreef clients`. The listing
    # has no reason to exist on its own — `GET /api/v1/clients` is the way to
    # look at clients, from a device that works. This one is for the operator
    # who cannot reach the API, and every such operator is about to revoke
    # something. One command to remember on a bad day beats two.
    revoke = sub.add_parser(
        "revoke",
        help="revoke a paired client, or list them",
        description=(
            "Turn off a client's credentials from the hub. The other half of "
            "replacing a phone: `bellasreef pair` lets the new one in, this turns "
            "the old one off. Both revoke endpoints need a live token of your own, "
            "which is exactly what a locked-out operator does not have."
        ),
    )
    revoke.add_argument(
        "client",
        nargs="?",
        metavar="ID|NAME",
        help="the client to revoke: its id, or its name when that is unambiguous",
    )
    revoke.add_argument(
        "--list",
        action="store_true",
        help="list every client this hub has ever paired, and revoke nothing",
    )
    revoke.add_argument("--json", action="store_true", help="machine-readable output")

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
    if args.command == "revoke":
        return _revoke_command(args, dsn)
    if args.command == "setup-code":
        return _setup_code_command(args, dsn)

    if args.ttl <= 0:
        print("--ttl must be greater than zero", file=sys.stderr)
        return 2

    result, audit_warning = asyncio.run(
        _open_window(dsn, args.ttl, os.environ.get("BELLASREEF_NATS_URL"))
    )
    if audit_warning:
        _warn(audit_warning)

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
    print()
    print(
        "If a code is already showing in the app, cancel and pair again — "
        "requests created before this window stay pending."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""`bellasreef` — the recovery CLI.

auth.md §1 calls this the fire escape, not the front door. It exists for one
situation: every paired client is lost or revoked, so there is nobody left to
approve a new one and the TOFU-ever window is shut by design.

It opens a **bounded pairing window** rather than clearing client state. That
distinction is the whole point — deleting revoked clients would reopen the
TOFU-ever window, which is keyed on rows having existed precisely so that
revoking everything cannot reopen open pairing. A recovery path that undid the
protection it is recovering from would be a much better attack than a feature.

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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

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

    args = parser.parse_args(argv)

    dsn = os.environ.get("BELLASREEF_DATABASE_URL")
    if not dsn:
        print("BELLASREEF_DATABASE_URL is not set", file=sys.stderr)
        return 2
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

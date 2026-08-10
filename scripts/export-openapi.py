# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Dump the API's OpenAPI document.

The spec is a published artefact (PRD §7.3.2) and the source of truth for every
client, so it is produced by the API itself rather than maintained by hand — a
hand-kept spec drifts from the server the first time someone is in a hurry.

No database is required: SQLAlchemy's create_async_engine does not connect
eagerly, and generating the document only walks the route table.

    uv run python scripts/export-openapi.py [--out openapi.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bellasreef_api.app import build_app
from sqlalchemy.ext.asyncio import create_async_engine

# Never used to connect. Present because build_app takes an engine, and a
# placeholder makes it obvious this is not talking to anything.
UNUSED_DSN = "postgresql+asyncpg://openapi-export/never-connected"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("openapi.json"))
    args = parser.parse_args()

    app = build_app(create_async_engine(UNUSED_DSN))
    spec = app.openapi()

    paths = spec.get("paths", {})
    if not paths:
        print("refusing to write an empty spec", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} — {len(paths)} paths, OpenAPI {spec.get('openapi')}")
    for path in sorted(paths):
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

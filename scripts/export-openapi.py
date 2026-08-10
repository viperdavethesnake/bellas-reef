# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Dump the API's published client contracts.

The spec is a published artefact (PRD §7.3.2) and the source of truth for every
client, so it is produced by the API itself rather than maintained by hand — a
hand-kept spec drifts from the server the first time someone is in a hurry.

No database is required: SQLAlchemy's create_async_engine does not connect
eagerly, and generating the document only walks the route table.

Two artefacts, published together because a client needs both:

* ``openapi.json`` — the REST surface.
* ``stream-frames.schema.json`` — the WebSocket frames. WebSockets are not
  expressible in OpenAPI 3.1, and PRD G3's footnote requires client frame
  types to be *generated* rather than hand-written, so the frame schema is a
  first-class artefact rather than prose in a README.

    uv run python scripts/export-openapi.py --out-dir .
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bellasreef_api.app import build_app
from bellasreef_api.frames import frame_json_schema
from sqlalchemy.ext.asyncio import create_async_engine

# Never used to connect. Present because build_app takes an engine, and a
# placeholder makes it obvious this is not talking to anything.
UNUSED_DSN = "postgresql+asyncpg://openapi-export/never-connected"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("openapi.json"))
    parser.add_argument("--frames-out", type=Path, default=Path("stream-frames.schema.json"))
    args = parser.parse_args()

    app = build_app(create_async_engine(UNUSED_DSN))
    spec = app.openapi()

    # Frame schemas are folded into components/schemas as well as published
    # standalone. swift-openapi-generator only consumes OpenAPI, and PRD G3's
    # footnote requires frame types to be GENERATED — so embedding them means
    # one generator and one toolchain rather than adding a second codegen path
    # for a handful of types. The standalone file stays for non-Swift clients.
    embedded = frame_json_schema(ref_template="#/components/schemas/{model}")
    components = spec.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in embedded.get("$defs", {}).items():
        components.setdefault(name, definition)

    paths = spec.get("paths", {})
    if not paths:
        print("refusing to write an empty spec", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} — {len(paths)} paths, OpenAPI {spec.get('openapi')}")
    for path in sorted(paths):
        print(f"  {path}")

    frames = frame_json_schema()
    definitions = frames.get("$defs", {})
    if not definitions:
        print("refusing to write an empty frame schema", file=sys.stderr)
        return 1
    args.frames_out.write_text(json.dumps(frames, indent=2, sort_keys=True) + "\n")
    print(
        f"wrote {args.frames_out} — {len(definitions)} definitions, "
        f"frame schema v{frames.get('x-frame-schema-version')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

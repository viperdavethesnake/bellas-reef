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


def _normalise_nullable(node: object) -> object:
    """Rewrite Pydantic's `anyOf: [X, null]` into OpenAPI 3.1 nullability.

    This is not cosmetic. swift-openapi-generator **silently drops** properties
    shaped as `anyOf: [{...}, {type: null}]` — no warning, no error, the field
    simply does not exist in the generated type. That cost us a client that
    could not read `SensorReading.value` or `StateFrame.override`: the
    temperature and the whole "override state is loudly visible" requirement,
    absent, with nothing to say so.

    Two shapes, handled differently:

    * `anyOf: [{type: number}, {type: null}]` becomes `type: [number, null]`,
      which is how OpenAPI 3.1 spells nullable and what generators expect.
    * `anyOf: [{$ref: X}, {type: null}]` collapses to the bare `$ref`. A type
      array cannot carry a reference, and the property is optional anyway —
      absent and null mean the same thing to a client here.
    """
    if isinstance(node, list):
        return [_normalise_nullable(item) for item in node]
    if not isinstance(node, dict):
        return node

    node = {key: _normalise_nullable(value) for key, value in node.items()}

    options = node.get("anyOf")
    if isinstance(options, list) and len(options) == 2:
        nulls = [o for o in options if o == {"type": "null"}]
        others = [o for o in options if o != {"type": "null"}]
        if len(nulls) == 1 and len(others) == 1:
            other = others[0]
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            if "$ref" in other:
                return {**rest, **other}
            if isinstance(other.get("type"), str):
                return {**rest, **other, "type": [other["type"], "null"]}
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("openapi.json"))
    parser.add_argument("--frames-out", type=Path, default=Path("stream-frames.schema.json"))
    args = parser.parse_args()

    app = build_app(create_async_engine(UNUSED_DSN))
    spec = app.openapi()
    spec = _normalise_nullable(spec)  # type: ignore[assignment]

    # Frame schemas are folded into components/schemas as well as published
    # standalone. swift-openapi-generator only consumes OpenAPI, and PRD G3's
    # footnote requires frame types to be GENERATED — so embedding them means
    # one generator and one toolchain rather than adding a second codegen path
    # for a handful of types. The standalone file stays for non-Swift clients.
    # Normalised too: they are folded in after the spec-wide pass, so they
    # would otherwise keep the anyOf-null shape the generator drops.
    embedded = _normalise_nullable(frame_json_schema(ref_template="#/components/schemas/{model}"))
    components = spec.setdefault("components", {}).setdefault("schemas", {})
    assert isinstance(embedded, dict)
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

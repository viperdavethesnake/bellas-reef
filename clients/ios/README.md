# iOS client — not in this repository

The Bella's Reef iOS app is **closed source** and lives in a separate private
repository. It is a paid App Store product; this directory is a pointer so the
public tree's layout matches the architecture.

Per PRD v1.1 Q3, that is a deliberate split, not an omission.

## What is public

Everything the app talks to:

- the OpenAPI spec — **Apache-2.0**, in this repository
- the NATS subject schema and payload models — **Apache-2.0**, `contracts/`

The app is generated from that spec via `swift-openapi-generator`, with no
hand-written bindings. So anything the iOS client can do, another client can
do, from published contracts alone, without permission and without inheriting
copyleft.

## Writing your own client

Start with `docs/contracts/nats-subjects.md` and the OpenAPI spec. Both are
versioned artefacts under Apache-2.0 — that is the entire point of publishing
them.

# Web UI (phase 5) — design

2026-08-14. Spec only, by David's ruling in session: **spec out the web UI,
then stop — it is not being built in this phase.** This document is the
deliverable; no implementation plan follows until David reopens the work.

"Phase 5" here means build-order item 5 in CLAUDE.md (clients, hardening,
publication) — the same deferral the PRD's locked-technology table records.
The PRD §10 numbered phasing uses a different numbering; its item 5 is
unrelated.

## Purpose and scope (ruled: structural config only)

R17 assigns the web UI the **structural-config surface**: what the iOS
design brief §4 keeps out of the app — hardware registration, channel
wiring, and lifecycle — plus the system-management surface both clients
share. Until this ships, structural config is done through Swagger, which
works but renders no safety copy, hides no footguns, and shows no state.

First iteration covers, in full:

- **Hardware**: announced capabilities, adopted devices, adopt/unadopt with
  the same safety confirm the iOS adoption UI ships, device rename.
- **Thresholds**: per-device alert min/max/clear-margin.
- **Clients**: paired-client list, revoke, revoke-self (sign out).
- **Audit**: the append-only log, filterable by category.
- **Hub info**: contracts version, health, connection state.

Explicitly not in this iteration: live dashboard, history charts,
overrides, schedules, calibration, any WebSocket use. See "Out of scope".

## Rulings pinned in session (David, 2026-08-14)

1. **Scope: structural config only.** Dashboard/history/overrides stay
   iOS-only for now.
2. **Origin model: the web container reverse-proxies the API.** One origin;
   zero API changes; no CORS anywhere. The alternative (static SPA + CORS
   on the API) touched the API's security surface and left the refresh
   token with nowhere to live but localStorage anyway.
3. **Refresh token: localStorage.** Zero contract change, survives browser
   restarts (the Keychain-equivalent behaviour). XSS-readable in principle,
   but the stack is plain HTTP on a LAN — the wire is already the weaker
   link, and the standing scope ruling is home-hobbyist, not adversarial.
   Revisit only if TLS/remote access (PRD Q2) ever lands.

## Architecture

One new compose service, `web`:

```
browser ──http──> web :8080 ──┬── static SPA files (built at image build)
                              └── /api/*  ──proxy──>  api:8000
```

- **Caddy** serves both roles: static file server for the built SPA,
  reverse proxy for `/api/*` (including the WebSocket upgrade on
  `/api/v1/stream` — one config line now, so the future dashboard
  iteration needs no infra change). Caddy over nginx for the ~10-line
  config and because if PRD Q2 ever rules in TLS, it is a one-line change
  rather than a certificate subsystem.
- Published `0.0.0.0:8080:8080` — the second LAN-published service, for the
  same reason as the API's 8000: clients have to reach it. All spine ports
  stay loopback-only.
- The API container is untouched. No CORS middleware, no cookie handling,
  no new endpoints, no contract change. Contracts stay at 3.6.0.
- **Discovery falls out for free**: a browser cannot do mDNS-SD browsing,
  but it does not need to — avahi already publishes `bellasreef.local`,
  and the SPA is served *by the hub*, so `http://bellasreef.local:8080`
  is both discovery and connection. No connect/URL-entry screen exists;
  same-origin `/api` is the only base URL.

## Stack

| Layer | Choice |
|---|---|
| Source | `clients/web/` (the existing placeholder) |
| Framework | React 19 + TypeScript (strict) + Vite |
| API client | **Generated**: `openapi-typescript` types + `openapi-fetch` runtime, from the committed `openapi.json`. No hand-written bindings (CLAUDE.md rule; same law as swift-openapi-generator on iOS) |
| Stream frames | `stream-frames.schema.json` → generated TS types — deferred with the WebSocket itself; noted so nobody hand-writes them later |
| Styling | Plain CSS (custom properties, dark-first per the iOS brief's non-goals — no design-system dependency) |
| Tests | Vitest; Playwright decision deferred to build time |
| Proxy/server | Caddy, digest-pinned multi-arch image |

React over lighter options because the structural-config surface is
form-heavy CRUD where the ecosystem (generated-client integration, form
state) pays for itself, and "build once, build right" favours the boring
durable choice. Recorded as a recommendation — David has not ruled on the
framework and may override before build.

## Auth flow (contract as-is, contracts 3.6.0)

- **Pairing**: TOFU is permanently shut (paired clients exist, revoked rows
  count). The web client always takes the code path: `POST /pair
  {client_name}` → 202 `{request_id, pairing_code, poll_after_s}` → display
  the code, poll `GET /pair/{request_id}` every ~5 s. The operator approves
  from an already-paired client (iOS) or the fire-escape CLI. Approval pays
  out the refresh token exactly once; a second poll gets 410, expiry is
  300 s, all-revoked-no-window is 403 with "run `bellasreef pair` on the
  hub" copy.
- **Tokens**: refresh token in localStorage; access JWT (TTL 900 s) in
  memory only. Every page load: silent `POST /token`, then straight to the
  app. 401 mid-session → one silent refresh-and-retry, then the pairing
  gate. Revocation is immediate server-side; the client treats a failed
  refresh as "you were revoked or the hub was wiped" and returns to the
  gate with that stated.
- **Sign out** = `DELETE /clients/me` plus clearing localStorage — an
  honest revocation, not just forgetting the token.

## Screens

**Pairing gate** (unauthenticated): client-name field (default
"Web — <browser>"), then the pairing code large and static with a poll
spinner and the expiry countdown. The three terminal endings render
distinctly: approved (proceed), denied/expired (410 copy + start over),
window-shut (403 copy naming the CLI fire escape).

**Hardware** (the reason this UI exists): mirrors the adoption-UI design's
placement law — inventory and lifecycle, never operation.

- Adopted devices first: display name, `source · channel`, driver, role
  badge, enabled state, poll interval. Row actions: rename (PATCH — the
  contract's `DeviceName` carries `display_name` only; poll interval and
  enabled are *displayed but not editable*, because no endpoint edits them
  in 3.6.0 — see open ruling 4), thresholds (below), **Unadopt** behind a
  confirm whose copy states the safe direction (engine stops commanding;
  history kept; re-adopt reattaches — unbind is soft-delete by design).
- Available channels below: `bound_to == null` capabilities as
  `source · channel` with the useful `detail` fields. Selecting one opens
  the adopt form: channel/driver fixed and displayed, name required,
  role picker constrained to what the contract allows (`w1-bus` → sensor,
  no role; PWM → `light`, sole legal choice rendered disabled-not-hidden).
- **Safety confirm on actuator sources only**, same copy as iOS: adopting
  starts real output as soon as the engine's schedule runs; only adopt
  bench-verified hardware. Sensors adopt without friction.
- The three refusal endings render verbatim as inline errors: 404 (channel
  no longer announced), 409 (claimed since the list loaded), 422 (role not
  legal).

**Thresholds** (per device): min / max / clear-margin form over
`GET`/`PUT /devices/{id}/thresholds`, with current values loaded first and
a stated distinction between "no thresholds set" and "failed to load".

**Clients**: the paired list with `this browser` marked. Revoke behind a
confirm; revoking self routes through the sign-out path. Copy states that
revocation is immediate and the pairing window does not reopen.

**Audit**: newest-first list over `GET /audit?limit&category`, category
filter, plain timestamps in hub-local time.

**Hub bar** (persistent): hub name, contracts version from
`GET /api/v1/info`, health from `/healthz`. If the hub's contracts version
differs from the version the client was generated against, say so loudly —
a version-skewed client is a stale-data bug wearing a working UI.

## Review law carried over from the iOS brief

§7.1, §7.2 and §7.7 of `docs/ios-design-brief.md` are platform-neutral and
bind this UI:

- **State completeness**: every fetch surface renders loading, empty,
  error, and stale explicitly. No spinner-forever, no blank-on-error.
- **Data honesty**: nothing invented, nothing interpolated. A list that
  failed to load says so; it does not show the previous answer as current
  without a stale marker.
- **Semantic colour law**: teal/amber/red carry meaning and are never used
  decoratively.

§7.3–7.6 (motion, haptics, Dynamic Type, glass) are iOS-specific and do
not port; web accessibility is standard semantics — real buttons, labels,
focus order — held to ordinary web standards, not invented parallel law.

## Deployment shape (described, not built)

- `deploy/Dockerfile.web`: multi-stage — pinned node image builds the SPA,
  pinned Caddy image serves it. `useradd 1000`, `USER 1000:1000`,
  `EXPOSE 8080`, `HEALTHCHECK` probing Caddy. Build context repo root,
  same as the other three.
- `deploy/compose.yaml`: `web` service with both `image:`
  (`ghcr.io/…-web:${BELLASREEF_TAG:-latest}`) and `build:`, ports
  `0.0.0.0:8080:8080`, `depends_on: api: condition: service_healthy`.
- CI: `web` joins the publish matrix (own cache scope, per the
  last-writer-wins comment) and gains a **contract-drift gate**: CI
  regenerates the TS types from `openapi.json` and fails on a dirty tree —
  the same byte-identity discipline the spec export already has.
- `scripts/deploy-pi.sh`: `web` joins `SERVICES`. The telemetry-on-the-wire
  gate is unchanged — the web container serves no telemetry and needs only
  its healthcheck.

## Testing (described, not built)

- Vitest: the auth state machine (gate ↔ app transitions, single
  refresh-retry on 401, revoked ending), and each screen's documented
  endings against a mocked `openapi-fetch` client — including
  409-after-staleness on adopt and the 410/403 pairing endings.
- The contract-drift typegen gate in CI is the contract test.
- No live-hub testing; the environment boundary applies to browsers too.
  A Playwright-against-compose-loopback smoke is a build-time decision.

## Open rulings for David (before any build)

1. **License for `clients/web/`.** The README's license table doesn't name
   `clients/`. Backend is AGPL-3.0-only, contracts Apache-2.0, iOS is
   closed and paid. The web UI would be the first open client — AGPL,
   Apache, or closed like iOS is a business decision, not a technical one.
2. **Calibration.** R17 names it web-first, but no calibration endpoint
   exists in contracts 3.6.0. Either it becomes API surface first or it
   drops from the web UI's charter; this spec excludes it rather than
   inventing endpoints.
3. **Framework confirmation.** React/Vite is recorded above as a
   recommendation, not a ruling.
4. **Device-edit surface.** `PATCH /devices/{id}` edits `display_name`
   only. If poll interval or enabled should be operator-editable, that is
   new contract surface (a wider patch model), not a UI decision — rule on
   whether it joins this unit's charter or stays adoption-time-only.

## Out of scope

Live dashboard and any WebSocket use (the proxy line ships ready for it),
history charts (min/avg/max band + honest-gap law applies when they come),
overrides, schedules (no API surface exists — lighting profiles are a file
mounted into control-engine; making them API is its own unit), calibration
(no API surface), TLS/remote access (PRD Q2, unchanged), CORS (made
unnecessary by the origin model), and any change to the API service or the
contracts.

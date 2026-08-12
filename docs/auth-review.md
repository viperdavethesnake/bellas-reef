# Auth end-to-end review — 2026-08-12

Four parallel read-only reviews: backend auth code, API surface conformance, the
iOS client, and process/test/documentation verification. Findings are
deduplicated, and the ones marked **verified** were re-checked directly against
the code rather than taken from a review agent.

Reviewed against `docs/contracts/auth.md`, which is the authoritative contract.

---

## The shape of the problem

Auth was designed once, carefully, and implemented in one direction only.

`auth.md` §2 numbers five steps, and every one of them is something the
*requesting* device does. The approving device appears once, inside a
subordinate clause: "while a paired client shows Approve/Deny." It never gets a
step, a payload, or an endpoint of its own. That unwritten actor is the one that
did not get built, and its absence is the root of four separate findings below.

The backend auth *logic* is the best-tested subsystem in this repo. Twenty-one
integration tests drive the real HTTP endpoints against real Postgres, starting
from zero credentials, and the database carries genuine invariants
(`revoked_iff_hash_cleared`) rather than code conventions. That work is sound
and should not be disturbed.

What surrounds it is not. Four of the seven journeys in §2 cannot be completed
by a real operator. The published contract is three minor versions stale. The
deploy gate proves the tank is monitored and never proves anyone can log in.

---

## Critical

### A1 — Pairing a second device is impossible, in three independent ways

Any one of these alone would block it. All three are present.

1. **No client can obtain a `request_id` to approve.** It is minted in
   `POST /pair` and returned only to the requesting device (`app.py:799-805`).
   There is no listing endpoint, no store method, no NATS event, and no stream
   frame carrying it to an approver. `approvePairing` and `denyPairing` are
   correct, tested, and unreachable from the published contract by construction.
2. **The iOS app never polls.** `PendingApproval` (`PairingFlow.swift:301-321`)
   is a `ProgressView` and three labels, with no `.task`, no timer, and no
   network call. It stores `requestId` and `pollAfter` and reads neither.
   `pollPairing` has zero call sites.
3. **The iOS app has no approver UI**, and could not have one, because of (1).

Consequence: a second device shows a plausible spinner forever. The server-side
tests at `test_auth_lifecycle.py:138-181` pass green against endpoints no
product surface calls.

### A2 — The recovery window can be spent more than once *(verified)*

`app.py:775` calls `await store.consume_window(...)` and discards the result.
The store method is atomic and correct, returning `False` when it loses the race
(`store.py:766-773`), but the handler minted the credential on the line above
and returns `PairGranted` regardless, with no rollback — unlike `approve_pairing`
(`store.py:835-841`), which does roll back.

Two concurrent `POST /api/v1/pair` during the window both receive permanent
refresh tokens. This is the one path where an unauthenticated LAN request yields
full operator rights, so it is exactly where single-use has to hold. Both
requests also emit `pair.window_used`, so the trail claims one window was spent
twice. `test_the_recovery_window_lets_exactly_one_client_in` is sequential and
cannot catch it.

### A3 — A lost sole phone cannot be revoked at all

Both `revokeSelf` and `revokeClient` require `Depends(current_client)`, and the
CLI has no `revoke` subcommand. With one paired device, a stolen phone's refresh
token stays valid indefinitely.

Opening a recovery window and pairing a replacement **does not revoke the
stolen phone**. The recovery path deliberately adds a client rather than
clearing state, which is correct for the TOFU-ever invariant, and leaves the
thief paired alongside you.

`auth.md:73`'s "lost phone = one tap" is true only for multi-device operators,
and A1 means no client can perform the tap anyway.

### A4 — A backup archive is a complete and permanent auth bypass

`backup.py` runs `pg_dump` over the whole database, so the archive contains
`signing_keys.secret` in plaintext plus every client id. Anyone holding one can
mint a valid JWT for any client. There is no encryption and no mode restriction
on the output file.

It cannot be remediated, because rotation does not exist: `signing_keys.retired_at`
is in the schema (`0003_auth_and_role.py:71`) and the read path already filters
on it, but nothing anywhere writes it. The API also caches the secret in a
process-local dict that is never invalidated (`app.py:568-572`), so even a
hand-rotated row would keep serving the old key until restart.

The manifest's omissions list documents what the archive lacks. What it contains
is not mentioned.

### A5 — An auth mutation with no audit row is undetectable by design

The audit row is produced by the *handler*, not by the *mutation*.
`Store.revoke()` (`store.py:462-478`) writes to `paired_clients` and emits
nothing; the `sink()` calls live in `app.py:952` and `:976`. Any other writer —
psql, the CLI, a migration, a future caller of `Store.revoke` — leaves no trace.

There is no backstop. The only trigger in the schema is
`trg_audit_log_immutable`, which protects existing rows and does nothing about
missing ones. No reconciliation job, no test comparing `revoked_at IS NOT NULL`
counts against `client.revoked` counts.

This is the mechanism behind the 2026-08-12 raw-SQL revocation. A test that
would catch it cannot be written against the current design without moving
emission into the store or adding a table trigger.

### A6 — A Keychain write failure after pairing locks the operator out permanently

`PairingFlow.swift:279-282` stores the refresh token inside the generic `catch`.
By the time it throws, the hub has already issued the credential and spent the
TOFU or recovery window. The token is discarded and never re-fetchable. Tapping
Pair again returns 202 into A1's dead spinner. Recovery requires SSH.

### A7 — The published contract is three minor versions stale *(verified)*

Committed `openapi.json` reports `info.version: 3.0.0`; the running app derives
`3.3.0`. `/api/v1/capabilities` is absent. `/api/v1/devices` has `get` and no
`post`. There are no `securitySchemes` anywhere in the file.

CI regenerates the spec and uploads it as a build artifact but never diffs it
against the committed copy, so drift is invisible. The iOS app's vendored spec
is byte-identical to the stale file, which means the generated Swift client has
no `bindDevice` method to call. The entire device-adoption flow is missing from
the artifact CLAUDE.md names as the source of truth for all clients.

The generated-client policy is supposed to make contract drift a compile error.
It does that only when the spec the client generates from is the spec the server
serves. Generation faithfully produced a correct client for a contract three
versions out of date, and every downstream check passed.

---

## Major

**B1 — A revoked client's open WebSocket streams forever.** `app.py:1523-1524`
checks `is_active` once, at connect, then loops `send_text` indefinitely with no
re-check. A revoked phone keeps receiving every sensor, state and alert frame
until the socket drops. `auth.md:88-90` promises ≤15 minutes of exposure; this
is unbounded. `StreamBridge._subscribers` is a bare `set[Queue]` with no client
identity, so there is currently nothing to close.

**B2 — Approval mints a live client row before the token can be delivered.**
`approve_pairing` writes a real row with a live hash and returns the plaintext
token, held only in a process-local dict (`app.py:487-508`). The unit is
`Restart=always`. A restart between approve and collect leaves a permanent
phantom client that inflates `active_client_count()`, so the hub believes an
approver exists when none does — the exact deadlock the recovery window exists
to break. The dict is unbounded and holds plaintext refresh tokens.

**B3 — `pair.window_opened` is skipped silently without `BELLASREEF_NATS_URL`.**
`cli.py:441` treats a missing NATS URL as a no-op: no warning, exit 0, identical
success banner. `/etc/bellasreef/api.env` is read by systemd, not by an
interactive SSH shell, and no doc tells the operator to export it. In the actual
recovery scenario, the most privileged operation in the system very likely
writes no audit row and does not say so. The API logs CRITICAL for exactly this
condition; the CLI has no equivalent.

**B4 — The signing secret can be created twice, and cannot be rotated.**
`store.py:56-79` is SELECT-then-INSERT under READ COMMITTED with no `FOR UPDATE`
and no unique constraint. Masked today only because the unit runs a single
uvicorn worker. `--workers 2` would make tokens minted by one worker rejected by
the other. `algorithm` is likewise dead: `security.py:48` hardcodes HS256.

**B5 — TOFU grant is an unserialized read-then-write.** `total_clients_ever()`
and `create_client()` run in separate transactions with no lock or constraint
between them (`app.py:758-770`). Two concurrent first-boot requests can both be
granted, with no record it was unintended.

**B6 — iOS refresh stampede.** `accessTokenNow()` is actor-isolated but suspends
at `await client.mintToken(...)` (`HubClient.swift:251-275`). Actors are
reentrant, so every caller arriving during that suspension passes the cache
guard and issues its own mint. Cold launch is at minimum five concurrent mints,
producing five `token.minted` audit rows per launch.

**B7 — iOS never recovers from a 401.** Refresh is proactive only, 60 s early.
No call site clears `accessToken` on `.unauthorized`. Clock skew, a restart, or
a regenerated signing key leaves the app resending a dead token for up to
fourteen minutes with no path to re-mint.

**B8 — Revoked mid-session leaves a permanently dead dashboard.** `TankMonitor`
catches `.unauthorized`, sets `.disconnected` and returns, killing the reconnect
loop, while `AppModel.phase` stays `.paired`. The user sees a frozen Tank tab
with no explanation and no route back to pairing. Relaunching recovers, which is
the only reason this is not critical.

**B9 — Every device pairs as "iPhone".** `PairingFlow.swift:158` uses
`UIDevice.current.name`, which returns the model on iOS 16+ without an
entitlement the project does not declare. `auth.md` §1's "Allow 'David's iPad'?"
becomes a blind tap, and a future clients list would show identical rows.

**B10 — No device removal endpoint.** Every other layer supports it:
`devices.adopted` is a real column, `DeviceAssignment` documents `adopted=False`
as the tombstone, and `factory.py:70` already builds nothing for an unadopted
assignment. Only the endpoint is missing, so a PWM channel bound to the wrong
device is permanently taken and the only recovery is SQL on the hub.

**B11 — Sixteen event types are filed under `auth`, not eleven.**
`NatsAuditSink` hardcodes `AUDIT_CATEGORY = "auth"` (`audit.py:38`), so
`device.bound`, `device.renamed`, `thresholds.set`, `override.created` and
`override.released` all land on `bellasreef.audit.auth`. `GET /audit?category=auth`
returns config and command events mixed with auth events.

**B12 — The deploy gate never touches auth.** `deploy-pi.sh` polls
VictoriaMetrics for a fresh sensor reading and dies loudly if none arrives,
which is a genuinely good check. Nothing curls `/api/v1/info`, nothing confirms
auth events reach `audit_log` on the real hub. Discovery or pairing could be
broken on the hub right now and the script would report success.

**B13 — `bellasreef pair` is untested and undocumented.** The string appears in
three places repo-wide, all narrative. Not in README, host-setup, or CLAUDE.md.
No doc states how to invoke it, which env vars it needs, or that `--ttl` exists.
The three recovery tests bypass the CLI entirely by calling
`Store.open_pairing_window()` directly, skipping argparse, validation, the
engine lifecycle, the operator output, and the audit emission.

---

## Minor

- **`deny_pairing` has no expiry guard** (`store.py:844-853`). An aged-out
  request can still be denied, returning 200 and sending the poller a 403 rather
  than a 410 — the wrong recovery instruction. `approve_pairing`'s guard is
  non-atomic for the same reason.
- **`expired` is an unreachable database state.** Nothing writes it; it is
  computed at read time only. No sweeper exists for `pairing_requests` or
  `pairing_windows`, and `POST /pair` is unauthenticated and inserts a row per
  call, so anyone on the LAN can flood the table and nobody can see or clear it.
- **Multiple recovery windows can be open at once**, only the newest is
  reachable, and there is no `--cancel`.
- **`client.revoked` has two payload shapes** — `actor`+`self` from self-revoke,
  `revoked_by` from revoke-other. A consumer keying on `revoked_by` silently
  misses every self-revoke.
- **Failed authentication emits nothing.** `token.rejected` covers the refresh
  path only; a forged bearer on any endpoint is silent, so there is no signal
  for probing.
- **`_noop_audit` logs at WARNING** when no sink is configured at all, while an
  actual publish failure logs CRITICAL. The worse case is the quieter one.
- **Unvalidated `limit`** on `listAudit` and `listAlerts` reaches Postgres
  directly and errors as an undeclared 500.
- **Six operations return bare `dict[str, str]`**, and `AuditEvent.event` is
  `dict[str, Any]`, which generate as opaque containers and push clients toward
  the hand-written code the project forbids.
- **`TokenStore` is not keyed by hub.** `kSecAttrLabel` is written on save and
  never matched on load, so exactly one credential exists for whichever hub
  paired last, and `isPaired()` would answer true for any hub URL.
- **Dead code:** `store.py:855-862` `take_pairing_token` has no callers and
  always returns `None`; `PairingOutcome` is exported and referenced nowhere.
- **Avahi advertises `contracts=2.0.0`** (`deploy/avahi/bellasreef.service:19`),
  hardcoded, three minors stale, derived from nothing and tested by nothing.
  `CONTRACTS_VERSION` in the API was fixed to derive from package metadata; the
  TXT record on step 1 of the auth flow was not included in that fix.

---

## Documentation

- **A client written from `auth.md` §2 gets a 422.** Every field name in the
  pairing example is stale: `device_name` (is `client_name`, and `PairRequest`
  is `extra="forbid"`), `device_id` (is `client_id`), `paired_device_count` (is
  `paired_client_count`), JWT claim `device_id` (is `client_id`). The doc
  violates its own naming rule in the four places an implementer would copy.
- **"`/info` and `/healthz` are the ONLY unauthenticated endpoints" is false**,
  in both `auth.md:48` and `prd.md:222`. Five are public. auth.md's own flow
  documents two of the others two lines later.
- **Half the auth surface is undocumented**: approve, deny, `DELETE /clients/me`,
  `approvers_available`, the `PairPending` shape, one-shot approval collection,
  WebSocket auth-by-first-message, and the deliberate property that revoked and
  unknown refresh tokens return an identical 401.
- **The code is stricter than the contract on revocation.** §3 says outstanding
  JWTs die at `exp`; `current_client` and the WS handshake both call
  `is_active`, so revocation is immediate on every authenticated route. The code
  is the safer side; the doc should be amended to match.
- **CLAUDE.md's locked stack says OAuth2** while `auth.md` lists OAuth2 flows as
  an explicit non-goal. PRD v1.2 was amended to fix this wording; CLAUDE.md was
  not. Flagged rather than resolved, per the document-authority rule.
- **The PRD lists auth as done and verified** (`prd.md:357`, `:387`), which
  predates two parked MAJOR auth defects in the session log. §12 exists so the
  PRD cannot silently claim more than the tree, and is currently doing exactly
  that.
- **S5.3's "5 conflicts flagged for ruling" against auth.md** are not enumerated
  anywhere and cannot be recovered from the log.

---

## What is good, and should survive any rework

Worth stating explicitly so a rewrite does not discard it.

- TOFU keyed on `total_clients_ever`, so revoking every client cannot reopen
  open pairing. The recovery window adds a client rather than clearing state,
  for the same reason, and `ondelete=RESTRICT` keeps the count honest.
- `revoked_iff_hash_cleared` as a database CHECK. "Revoked implies no usable
  hash" is an invariant, not a convention.
- Refresh tokens stored as SHA-256 of a 256-bit random value, with the reasoning
  for not using a KDF written down and correct.
- `algorithms` pinned in `verify_access_token`, so a token asking to be verified
  with `none` gets no hearing.
- All four `/pair` outcomes implemented in the documented precedence order, and
  the 403 branch that refuses to promise an approval nobody can give.
- The iOS Keychain accessibility class is right on both axes:
  `AfterFirstUnlock` so reconnect works while locked, `ThisDeviceOnly` so a
  restore to a new phone correctly does not carry the pairing.
- Cold launch with a valid token and no network goes straight to the dashboard
  and reports disconnected honestly. No spinner, no false all-clear.
- `test_an_auth_event_reaches_the_audit_log_and_the_api` waits up to 60 s for a
  real row through the real chain. Its docstring names the failure mode this
  project keeps hitting: "every link in this chain existed and passed its own
  tests while the chain itself was broken."
- The API client is genuinely generated. `Generated.swift` is 22 lines of
  comment and no code, the only hand-written transport is the documented
  WebSocket exception, and it decodes exclusively into generated schema types.

---

## Journey matrix

| Journey (auth.md §2) | Backend | iOS | Operator can do it |
|---|---|---|---|
| Pair first client (TOFU) | tested from zero | implemented | **yes** |
| Approve second client | tested, unreachable | absent | **no** (A1) |
| Deny a request | tested, unreachable | absent | **no** (A1) |
| Recovery window | CLI bypassed by tests | message only | probably, unverified (B13) |
| Token refresh | tested | proactive only | yes, with B6/B7 |
| Revoke self | tested | good UX | **yes** |
| Revoke another client | tested | absent | **no** (A1, A3) |
| Rotate signing secret | not built | n/a | **no** (A4) |

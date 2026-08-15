# Auth & pairing — v2

**Status:** active · **Owner:** David / Bella's Reef LLC
**Scope:** how a client (iOS app, web UI) discovers the hub, pairs, and
authenticates. Single operator, no accounts, no cloud. Paired means trusted.

One page. If a change makes this longer, the change is probably wrong.

### Changelog

- **2.1 (2026-08-15)** — **setup mode**: `POST /pair` gains an optional
  `setup_code`, so a new owner's first device pairs by typing a code the hub
  printed rather than by winning a race on the LAN. §1a. Blind TOFU survives
  unchanged on a hub where no code was ever minted.
- **2.0 (2026-08-12)** — second-device pairing becomes **code-based**. v1
  specified approve-from-paired and it was never completable: no client could
  obtain a `request_id` to approve, so `POST /pair/{id}/approve` was unreachable
  from the published contract by construction. See `docs/auth-review.md` and
  `docs/superpowers/specs/2026-08-12-auth-end-to-end-design.md`. This version
  also corrects field names that were wrong in every v1 example — a client
  written from v1's §2 got a 422 on its first call — and the false claim that
  only two endpoints are unauthenticated.

---

## 1. Model

**TOFU (trust on first use), then pairing by code.**

- While the hub has **zero** paired devices, the first `POST /pair` on the LAN
  succeeds and the open window closes. The trust assumption is a private home
  LAN for the minutes between first boot and first app launch.
- Every later device shows a **six-digit code**, which the operator types into
  an already-paired device (System → Add a device). The existing device is the
  trust anchor; holding the new device and reading its code is the physical
  presence.

  The code is **not a secret defending the hub** — `POST /pair/claim` requires a
  bearer token, so only an already-paired device can approve anything, and
  guessing a code gains an attacker nothing they do not already hold. It is a
  *selector*, proving the operator is looking at the device that is asking.
  Hence no rate limiter and no attempt counter.

  v1 had the paired device approve a named request instead. Two things sank it.
  Nothing told the operator a request was waiting, and `UIDevice.current.name`
  returns the model on iOS 16+ without an entitlement we do not declare, so the
  prompt read "Allow 'iPhone'?" and identified nothing. A code inverts the
  initiative: the operator starts from the device already in their hand, so
  neither discovery nor identity is a problem to solve.
- **Recovery (fire escape, not front door):** all clients lost or revoked →
  SSH to the hub, `bellasreef pair` opens a bounded **pairing window**
  (default 5 min). This CLI is the only terminal interaction in the system
  and exists only for recovery.

  **As built:** the CLI writes a `pairing_windows` row; it does **not** clear
  client state. That distinction matters — the TOFU-ever window is keyed on
  client rows having existed, precisely so that revoking every client cannot
  reopen open pairing. Deleting revoked clients to "reset" would undo the
  protection the recovery is recovering from. A window is spent by the first
  client that uses it, or expires on its own.

  A window **adds** a client; it does not remove one. Replacing a lost phone is
  therefore two commands: `bellasreef pair` to let the new device in, and
  `bellasreef revoke` to turn the old one off. Both run on the hub, and they
  are the only terminal interactions in the system.

No usernames, no passwords, no scopes, no roles, no OAuth2 authorization flows.
A paired device has full operator rights. Revocation is the only privilege
operation, and any paired device can do it.

## 1a. Setup mode

A hub that has never completed a pairing is **in setup mode**
(`hub_identity.setup_completed_at` IS NULL; `/info` reports `setup_mode`). On
such a hub `bellasreef setup-code` — run by `scripts/deploy-pi.sh` and
`scripts/factory-reset-pi.sh`, and by hand over SSH — mints an eight-character
code, prints it once (grouped for reading as `7KF2-9QMD`; the dash and case are
ignored on entry), and stores **only its hash**. There is no plaintext to
reprint from: "I forgot it" is answered by minting a new one, which rotates the
old out. The first pairing completes setup, and the code stops meaning anything.

`POST /api/v1/pair` takes an optional `setup_code`. Three outcomes, and none of
them is a silent ignore:

- **Valid, in setup mode** → 200 `{ refresh_token, client_id }` immediately, by
  the same store call the TOFU and window paths use — one way to mint a client,
  not a fourth. Setup completes. Audited as `pair.code_granted`.
- **422**, with the reason spelled out for the operator, in two cases: a code
  that is missing or wrong while this hub is in setup mode *and* a code has been
  minted, and a code supplied to a hub that is already set up. A rejection, not
  a pending request — during setup there is nobody yet to approve one. A wrong
  code is audited as `pair.code_rejected`.
- **429** with `Retry-After` after **10 failed attempts in a minute**. Unlike
  the six-digit pairing code of §1, this one *is* a secret defending the hub,
  so it is counted and throttled.

**Precedence during setup mode:** an open recovery window wins over the code
gate. A code-less `POST /pair` with a window open is granted by the window path
even while a minted code is outstanding — `bellasreef pair` is the fire escape
and a minted code narrows the unauthenticated blind-TOFU path, not the
operator's own way back in. Recorded as amendment (b) in
`docs/superpowers/specs/2026-08-15-new-owner-experience-design.md`; amendment
(a) covers the `method` field below.

A hub deployed before this existed, where no code was ever minted, still
bootstraps by blind TOFU exactly as §1 describes.

## 2. Flow

```
1. DISCOVER   Bonjour browse for _bellasreef._tcp        (avahi publishes it;
              → found: "Bella's Reef (bellasreef.local)"  hostname alone is
              → not found: manual IP entry field          not enough — the
                                                          service type must be
                                                          registered)

2. IDENTIFY   GET /api/v1/info                     [unauthenticated]
              → { name, api_version, contracts_version, paired_client_count,
                  pairing_open, approvers_available }
              Renders the connect screen before any commitment. /info returns
              nothing sensitive. Note `paired_client_count` counts clients
              EVER paired, including revoked ones — that is what keeps the
              TOFU window shut; `approvers_available` is the live count.

              The unauthenticated set is exactly: /healthz, /api/v1/info,
              POST /api/v1/pair, GET /api/v1/pair/{id}, POST /api/v1/token.
              Everything else requires a bearer. (v1 claimed two. It listed
              three of the other three itself, two lines later.)

3. PAIR       POST /api/v1/pair { client_name, setup_code? }
              → setup_code, hub in setup mode : 200 + token, setup completes
                           anything else about a code: 422, or 429 after ten
                           failures in a minute (§1a)
              → count==0 : 200 { refresh_token, client_id }    window closes
              → count>0  : 202 { request_id, pairing_code, poll_after_s,
                                 expires_in_s }
                           New device DISPLAYS pairing_code and polls
                           GET /api/v1/pair/{request_id} (~every 5 s, 5-min
                           expiry). Operator types the code into a paired
                           device, which calls POST /api/v1/pair/claim.
                           Approved → poller gets 200 + token, once.
                           A second poll gets 410: one credential per approval.
                           Poll, not push: push infra does not exist and a
                           30 s wait during a once-ever pairing is fine.
              → recovery window open : 200 + token, window spent
              → clients exist but ALL revoked, no window : 403
                           nobody can approve, so say so rather than leave
                           the app polling a request no one will ever see.
                           app shows: "run `bellasreef pair` on the hub"

3a. CLAIM     POST /api/v1/pair/claim { code }              [authenticated]
              → 200 { request_id }   matched a pending request; approved
              → 404                  no pending request carries that code
              → 409                  that request is no longer pending
              Thin resolver in front of POST /api/v1/pair/{id}/approve, which
              (with /deny) remains the underlying operation.

4. TOKEN      POST /api/v1/token { refresh_token }
              → { access_token, expires_in }
              access_token: JWT, ~15 min. refresh_token: long-lived, bound to
              the client row, stored in iOS Keychain, dies only on revocation.
              Every app launch: silent step 4, then authenticated traffic.

5. USE        Authorization: Bearer <jwt> on all other endpoints.
              GET  /api/v1/clients                list paired clients by name
              DELETE /api/v1/clients/me           unpair this device
              DELETE /api/v1/clients/{id}         revoke another device
              WS   /api/v1/stream                 live state+sensor fan-out
              ... (remaining surface per the OpenAPI spec)

              Revoking someone else needs a live token of your own. An
              operator whose ONLY device is lost has none, so recovery is
              `bellasreef revoke` on the hub. Opening a recovery window and
              pairing a replacement does NOT revoke the lost device — the
              window adds a client rather than clearing state, deliberately
              (§1), so the two commands are the two halves of replacing a
              phone.
```

Steps 1–4 happen once per device, ever. Every subsequent launch is a silent
token refresh and straight to the dashboard.

## 3. Token rules

- Refresh token: opaque random (≥256-bit), stored **hashed** server-side, one
  per device row. Presenting it is the only way to mint JWTs.
- Access JWT: short-lived (~15 min), signed with a server-side key generated at
  first boot and stored in Postgres. Claims: `device_id`, `exp`, `iat`. Nothing
  else — there are no roles to claim.
- Revoking a client deletes its refresh-token hash, so no new JWTs can be
  minted. **As built, revocation is also immediate**, not `exp`-bounded:
  `current_client` and the WebSocket handshake both check liveness, so an
  outstanding JWT stops working on the next request. v1 documented the weaker
  ≤15-minute guarantee; the code is the safer side and this records it.
  No introspection endpoint and no denylist — that machinery buys nothing when
  a liveness check is one query.

  **One exception, accepted:** a WebSocket that was already open keeps
  streaming until the socket drops, because liveness is checked at connect and
  the send loop does not re-check. In a home the revoked client is your own old
  phone. See the accepted-risk table in the auth design spec.
- All auth events publish to `bellasreef.audit.auth` per the existing audit
  contract. **As built:** `pair.tofu_granted`, `pair.requested`,
  `pair.approved`, `pair.denied`, `pair.collected`, `pair.no_approver`,
  `pair.window_opened`, `pair.window_used`, `pair.code_granted`,
  `pair.code_rejected`, `token.minted`, `token.rejected`, `client.revoked`.

  The four events that grant a credential carry a `method`:
  `pair.code_granted` → `"setup_code"`, `pair.window_used` → `"window"`,
  `pair.approved` → `"approval"`, `pair.tofu_granted` → `"tofu"`. One field on
  the event that already exists per path, rather than a second `client.paired`
  row layered on top of it — amendment (a) in the new-owner spec, which is also
  where `"tofu"` joins the spec's original three values. `pair.code_rejected`
  carries the client name and never the code.
- Publishing failure is logged at CRITICAL but does **not** fail the request.
  An auth event missing the trail is bad; being locked out of your own tank
  by a logging problem is worse.

> **Naming:** paired phones and tablets are *clients*. `devices` is already
> taken by sensors and actuators — the Postgres table, the NATS subjects and
> the driver contract all mean hardware by it. One word, two meanings, on
> adjacent API surfaces was going to cost someone an afternoon.

## 4. Explicit non-goals

Multi-operator accounts, RBAC, OAuth2 authorization-code/PKCE, third-party
identity, scopes, per-endpoint permissions, cloud-relayed pairing. Single
operator controlling reef lights; the ceiling is the PRD, not the sky.

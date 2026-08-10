# Auth & pairing — v1

**Status:** draft for review · **Owner:** David / Bella's Reef LLC
**Scope:** how a client (iOS app, web UI) discovers the hub, pairs, and
authenticates. Single operator, no accounts, no cloud. Paired means trusted.

One page. If a change makes this longer, the change is probably wrong.

---

## 1. Model

**TOFU (trust on first use) with approve-from-paired thereafter.**

- While the hub has **zero** paired devices, the first `POST /pair` on the LAN
  succeeds and the open window closes. The trust assumption is a private home
  LAN for the minutes between first boot and first app launch.
- Every later device is approved from an **already-paired** device
  ("Allow 'David's iPad'? → Approve"). The operator's existing device is the
  trust anchor; physical presence is the approve tap.
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

No usernames, no passwords, no scopes, no roles, no OAuth2 authorization flows.
A paired device has full operator rights. Revocation is the only privilege
operation, and any paired device can do it.

## 2. Flow

```
1. DISCOVER   Bonjour browse for _bellasreef._tcp        (avahi publishes it;
              → found: "Bella's Reef (bellasreef.local)"  hostname alone is
              → not found: manual IP entry field          not enough — the
                                                          service type must be
                                                          registered)

2. IDENTIFY   GET /api/v1/info                     [unauthenticated]
              → { name, version, paired_device_count }
              Renders the connect screen before any commitment. /info and
              /healthz are the ONLY unauthenticated endpoints; /info returns
              nothing sensitive.

3. PAIR       POST /api/v1/pair { device_name }
              → count==0 : 200 { refresh_token, device_id }    window closes
              → count>0  : 202 pending; new device polls GET /api/v1/pair/{req_id}
                           (~every 5 s, 5-min expiry) while a paired client
                           shows Approve/Deny. Approve → poller gets 200+token.
                           Poll, not push: push infra does not exist yet (Q1)
                           and a 30 s wait during a once-ever pairing is fine.
              → recovery window open : 200 + token, window spent
              → clients exist but ALL revoked, no window : 403
                           nobody can approve, so say so rather than leave
                           the app polling a request no one will ever see.
                           app shows: "run `bellasreef pair` on the hub"

4. TOKEN      POST /api/v1/token { refresh_token }
              → { access_token, expires_in }
              access_token: JWT, ~15 min. refresh_token: long-lived, bound to
              the device row, stored in iOS Keychain, dies only on revocation.
              Every app launch: silent step 4, then authenticated traffic.

5. USE        Authorization: Bearer <jwt> on all other endpoints.
              GET  /api/v1/clients                list paired clients by name
              DELETE /api/v1/clients/{id}         revoke (lost phone = one tap)
              WS   /api/v1/stream                 live state+sensor fan-out
              ... (remaining surface per the OpenAPI spec)
```

Steps 1–4 happen once per device, ever. Every subsequent launch is a silent
token refresh and straight to the dashboard.

## 3. Token rules

- Refresh token: opaque random (≥256-bit), stored **hashed** server-side, one
  per device row. Presenting it is the only way to mint JWTs.
- Access JWT: short-lived (~15 min), signed with a server-side key generated at
  first boot and stored in Postgres. Claims: `device_id`, `exp`, `iat`. Nothing
  else — there are no roles to claim.
- Revoking a device deletes its refresh-token hash; outstanding JWTs die at
  `exp` (≤15 min exposure). No token introspection endpoint, no denylist —
  that machinery buys nothing at this scale.
- All auth events publish to `bellasreef.audit.auth` per the existing audit
  contract. **As built:** `pair.tofu_granted`, `pair.requested`,
  `pair.approved`, `pair.denied`, `pair.collected`, `pair.no_approver`,
  `pair.window_opened`, `pair.window_used`, `token.minted`,
  `token.rejected`, `client.revoked`.
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

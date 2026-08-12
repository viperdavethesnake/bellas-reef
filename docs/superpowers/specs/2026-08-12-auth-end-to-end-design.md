# Auth end-to-end — design

**Date:** 2026-08-12 · **Status:** approved · **Supersedes:** nothing
**Findings this answers:** `docs/auth-review.md`

---

## 1. The goal, stated once

**Nobody gets locked out of their own tank.**

Every item below earns its place by closing a lockout path, or by being cheap
enough to fix while we are already in the file. Security work that does not
close a lockout is recorded as accepted risk in §6 rather than built.

This threshold is deliberate and it is a scope ruling, not a shortcut. The
product is for home reef keepers on a private LAN. They do not have a targeted
attacker; they have a tank that dies if they lose access to it. The review that
preceded this design ranked its findings on the wrong threat model, calling
races and archive contents critical while filing "a lost phone locks you out
forever" as merely major. §6 records what we are choosing not to defend against,
so that the choice is visible rather than implied.

## 2. Pairing by code

### Why not approve-from-paired

`auth.md` v1 specified an approve-from-paired flow and it was never completable.
Three independent blockers, any one sufficient: no client can obtain a
`request_id` to approve, the app never polls, and no approver UI exists. Beyond
those, two properties made it a poor fit even if built. Nothing tells the
operator a request is waiting, so it needs either a badge they happen to notice
or new stream plumbing. And `UIDevice.current.name` returns the model on iOS 16+
without an entitlement this project does not declare, so the prompt reads
"Allow 'iPhone'?" and identifies nothing.

A code inverts the initiative. The operator starts from the device already in
their hand, so discovery stops being a problem, and identity stops being a
problem because they are looking at the device that shows the code.

### Mechanics

`POST /api/v1/pair` gains one field in its **202** body:

```
202 { request_id, pairing_code, poll_after_s, expires_in_s }
```

`pairing_code` is six digits, generated at request creation, unique among
requests currently in `pending`. Uniqueness is a partial unique index, not a
retry loop.

The requesting device displays the code and polls `GET /api/v1/pair/{request_id}`
on the cadence it is already told. That poller does not exist today and is part
of this work.

The paired device gains **System → Add a device**: one six-digit field. It calls

```
POST /api/v1/pair/claim  { code }        [Bearer]
  200  { request_id }        code matched a pending request; approved
  404                        no pending request carries that code
  409                        the request is no longer pending
```

`claim` resolves the code to a request and calls the existing `approve_pairing`
store method. `POST /pair/{id}/approve` and `/deny` are unchanged — they are
correct and tested, and claim is a thin resolver in front of them.

### Why there is no rate limiter

`claim` requires a bearer token, so only an already-paired device can approve
anything. Guessing a code therefore gains an attacker nothing they do not
already hold. The code is not a secret defending the hub; it is a **selector**
proving the operator is physically looking at the new device. No attempt
counter, no lockout, no brute-force surface to reason about.

### Why there is no pending-request list

Considered and dropped. Its only remaining job would be showing requests the
operator did not initiate, and since those can never become credentials, they
are litter rather than risk. Litter gets a sweeper (§5), not a screen.

## 3. Lockout fixes

**`bellasreef revoke`** — a CLI subcommand: list clients, revoke one by id or
name, emit `client.revoked`. This is what lets the fire escape *replace* a lost
phone instead of only adding a device beside it. Recovery deliberately adds a
client rather than clearing state, to protect the TOFU-ever invariant, and that
reasoning stays; this gives the operator the other half. It also retires the
last reason anyone would revoke in SQL, which is how the 2026-08-12 audit gap
happened.

**Keychain pre-flight** — probe with a write-and-delete *before* calling
`POST /pair`, so a Keychain failure happens before the hub spends its TOFU or
recovery window rather than after. A failure at store time then reports the real
error rather than falling into `PairingFlow`'s generic catch. Today this
sequence leaves the operator permanently locked out with SSH as the only
recovery.

**A route back to pairing** — on `.unauthorized` from `mintToken`, clear the
token store and set `phase = .choosingHub`, landing the user on the pairing
screen with an explanation. Today `TankMonitor` catches it, sets `.disconnected`
and returns, killing the reconnect loop while `phase` stays `.paired`. The user
sees a frozen dashboard and the only recovery they would find is delete and
reinstall.

**`DELETE /api/v1/devices/{device_id}`** → 204, Bearer. Sets `adopted=false`,
publishes `DeviceAssignment(adopted=False)` — the tombstone the contract already
defines and `factory.py` already honours — and audits `device.unbound`. Soft,
preserving telemetry identity and history. Without it a PWM channel bound to the
wrong device is taken forever, because `bindDevice` returns 409 on a bound
actuator channel.

**`bellasreef pair` as a procedure** — invocation, required environment
(`BELLASREEF_DATABASE_URL` mandatory, `BELLASREEF_NATS_URL` for the audit
event), `--ttl`, worked example. In README and `docs/host-setup.md`. It appears
in three places today, all narrative, none of them instructions.

## 4. Riding along

- **Check `consume_window`'s result** and return 409 on the lost race, instead of
  discarding the boolean and minting a second credential.
- **`chmod 600` on backup archives**, plus a manifest line stating the archive
  contains the JWT signing secret. The omissions list documents what an archive
  lacks; what it carries is more important.
- **CI diffs the generated spec against the committed one.** This is the
  mechanical cause of the app being unable to adopt hardware: the committed
  `openapi.json` is at 3.0.0 with no `POST /devices` and no `/capabilities`, the
  app's vendored copy is byte-identical, so the generated client has no method
  to call. Regenerate to a temp file, `diff`, fail on drift.
- **An auth leg in `deploy-pi.sh`** — curl `/api/v1/info`, assert it answers and
  reports the expected contracts version. The deploy gate proves the tank is
  monitored and never proves anyone can log in.
- **`auth.md` corrections** — field names (`client_name`, `client_id`,
  `paired_client_count`, JWT claim `client_id`), the false "only two
  unauthenticated endpoints" claim, the undocumented approve/deny/`clients/me`
  endpoints, and revocation being immediate rather than `exp`-bounded.

## 5. Hygiene

A sweeper for `pairing_requests` and `pairing_windows`. Aged-out rows currently
sit as `pending` forever — `expired` is a state nothing ever writes — and
`POST /pair` is unauthenticated and inserts a row per call, so the tables grow
without bound. Delete on read or on a timer; either is fine, neither is
interesting.

## 6. Accepted risk

Recorded so the choice is visible. Revisit if the product's shape changes.

| Risk | Why accepted |
|---|---|
| A revoked client's open WebSocket streams until the socket drops | In a home, the revoked client is your own old phone. Fixing it means per-client subscriber identity in `StreamBridge` for a case that is not adversarial here. |
| Backup archives are unencrypted and carry the signing secret | The archive is a file on the operator's own machine. Treated as a password-manager export: mode-restricted and documented, not encrypted. |
| No signing-key rotation | `retired_at` exists and the read path is rotation-ready, so the door is open when there is a reason. There is no leak to remediate and no compliance driver. |
| TOFU and signing-key creation races | Latent behind a single uvicorn worker. Revisit before ever adding `--workers`. |
| Failed authentication attempts are not logged | A probing signal matters when there is someone to probe. Not here. |

## 7. Testing

One rule, and it is the whole lesson of the review:

> **A journey test may not share state between participants.**

Today's approval test passes because it keeps the `request_id` from the pairing
call and hands it to the approver. A real approver never has that, which is
precisely why the journey is uncompletable in production while the test is
green. The auth suite is the best-tested code in the repo and it still shipped
an unreachable journey, because the test held both sides of a two-party
conversation at once.

The new journey test uses two clients, each restricted to what that participant
could actually obtain: the new device gets a code and nothing else; the approver
gets only what a person could read off a screen and type.

Also in scope: tests for `bellasreef pair` and `bellasreef revoke` through
`main()` rather than by calling store methods directly, and an assertion that
`pair.window_opened` is emitted.

## 8. Out of scope

Lighting schedules are configured by a JSON file on the Pi, read once at process
start, with no endpoint to read or edit them and no reload path. That is a real
API-first violation and it collides with the deployment rule, but it is not auth
and it is a larger decision than an endpoint shape. Recorded here so it is not
lost; not addressed by this spec.

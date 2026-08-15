# New-owner experience: setup code + factory reset

Approved by David 2026-08-15. Two features that compose: a first-pair
bootstrap that removes the SSH requirement from a new owner's first app
setup, and a factory-reset script that turns the 2026-08-14 manual wipe
into one audited command. Together they make "deploy → pair → adopt" and
"wipe → deploy → pair → adopt" both walkable without tribal knowledge.

Security posture, ruled: this is a hobbyist reef controller on a home LAN,
not an enterprise product. Sound, not inflated. The authorization boundary
for every privileged operation remains SSH/physical access to the hub;
nothing here is reachable from off the hub except the one thing designed
for it (pairing with a setup code, on the LAN, while the hub has never
paired anyone).

## Motivation

Today the only way to open a pairing window is `bellasreef pair` over SSH.
A new owner who deploys the stack and comes back later to set up the iOS
app dead-ends at "SSH back in." The gap is exactly one moment: the FIRST
client on a hub that has never paired anyone — the existing approver
screen already covers every later device, and the CLI window remains the
fire-escape. Separately, wiping the hub to factory state is undocumented
volume surgery (five commands, one point-of-no-return, measured 2026-08-14).

## Feature 1 — setup code

### Semantics

- **Setup mode** ⇔ no client has ever paired. Tracked in `hub_identity`
  by `setup_completed_at TIMESTAMPTZ NULL` — set at the first successful
  pair (any method), never unset. Revoking every client later does NOT
  re-enter setup mode: the fire-escape (`bellasreef pair`) covers
  lost-everything, and a long-forgotten printed code must not quietly
  become a key again.
- **The code**: 8 characters from a confusable-free alphabet (Crockford
  base32 minus 0/O/1/I), displayed grouped as `7KF2-9QMD`, ~40 bits.
  Case-insensitive on entry; the grouping dash is cosmetic and ignored.
- **Stored hashed** (same construction as refresh-token hashing in
  `security.py`), in `hub_identity.setup_code_hash TEXT NULL`. Hashing
  means "I forgot" is answered by rotating, not reprinting — same SSH
  trip either way, no plaintext at rest. Exactly one code is valid at a
  time; minting a new one invalidates the old.
- **Throttling**: failed setup-code attempts are limited globally
  (10 failures/minute → 429 with Retry-After). State may be in-process
  memory — a restart resetting the throttle is acceptable at this threat
  model. No lockout, no counters in the database.

### Pairing protocol (contract 3.7.0, semver-minor)

- `GET /api/v1/info` (unauthenticated; discovery already calls it) gains
  `setup_mode: bool`.
- `POST /api/v1/pair` request gains optional `setup_code: str | null`.
  - In setup mode with a valid code: **granted immediately** — the
    response is the same `PairGranted` shape the window flow returns.
    No window, no approval, no pending state.
  - In setup mode with a missing/invalid code: explicit rejection
    (422 with a reason the app can show). NOT a pending request — there
    is nobody to approve it.
  - Outside setup mode: a non-null `setup_code` is rejected the same
    way. The field is never silently ignored.
- The window flow (`bellasreef pair`) and the bearer-authed approver flow
  (`POST /api/v1/pair/claim`) are untouched, and both still work during
  setup mode.
- The `client.paired` audit event gains a `method` field:
  `"setup_code" | "window" | "approval"`.

### CLI

- New subcommand `bellasreef setup-code`:
  - In setup mode: mint a new code (rotating out any previous one), store
    the hash, print the code with the copy "Open the Bella's Reef app on
    this network and enter this code when asked."
  - After first pair: print "Setup is complete. Pair new devices from the
    approver screen on an already-paired device, or open a window with
    `bellasreef pair` as the fire-escape." Exit 0 — informational, not an
    error.
- `bellasreef pair` output gains the UX-6 sentence: "If a code is already
  showing in the app, cancel and pair again — requests created before
  this window stay pending." (Closes finding UX-6 from the 2026-08-14
  iOS UX review.)

### Deploy integration

`scripts/deploy-pi.sh` ends by querying setup state (compose exec) and,
if the hub is in setup mode, running `bellasreef setup-code` and printing
its block as the final output of the deploy. Every deploy in setup mode
rotates the code; harmless before the first pair, impossible after it.

### Migration

Alembic 0017: add `setup_code_hash TEXT NULL` and
`setup_completed_at TIMESTAMPTZ NULL` to `hub_identity`. Backfill for
existing hubs: any hub with one or more rows in `paired_clients` gets
`setup_completed_at = now()` at migration time, so already-set-up hubs
never re-enter setup mode.

## Feature 2 — iOS onboarding (bellasreef-ios repo)

- Re-pin contracts at 3.7.0; the generated client picks up `setup_mode`
  and `setup_code` (PRD G3: drift is a compile error).
- After "Find your hub," when `/info` reports `setup_mode: true`, the
  next step is a code-entry screen: "Enter the setup code from your
  deploy terminal," with the 4-4 grouping mirrored in the field. A valid
  code lands directly in the paired state. Rejection shows the server's
  reason and allows retry (the throttle's 429 surfaces as "too many
  attempts — wait a minute").
- When `setup_mode: false`, today's request-and-wait flow is unchanged.
- Design-brief laws apply as everywhere: §7.1 states for the entry screen
  (idle, submitting, rejected, throttled), amber for errors, never red.

## Feature 3 — factory-reset script

`scripts/factory-reset-pi.sh`, sibling to `deploy-pi.sh`, encoding the
2026-08-14 manual sequence:

1. Mandatory backup: `bellasreef backup --out
   /backups/bellasreef-pre-factory-<timestamp>.tar.gz` via compose exec;
   abort if it fails. No skip flag.
2. Print what will be destroyed (the three volumes, telemetry history,
   audit log, all pairings) and require the operator to type
   `factory-reset` verbatim.
3. `sudo systemctl stop bellasreef.service`, then compose `down` (the
   stopped containers still hold volume references — measured 2026-08-14),
   then `docker volume rm bellasreef_postgres-data bellasreef_vm-data
   bellasreef_nats-data`.
4. `deploy-pi.sh --no-verify` — recreates volumes, migrates from zero,
   starts the boot unit. `--no-verify` is correct here by construction:
   the telemetry gate cannot pass on an empty registry (no devices → no
   readings; the 2026-08-12 cutover and the 2026-08-14 reset both hit
   this).
5. Verify fresh state and print it: 0 devices, 0 paired clients, 0 audit
   rows, alembic at head, all six JetStream streams recreated,
   capabilities announced by hardware-io.
6. Print the new setup code (deploy-pi.sh already did, via Feature 1 —
   the script just makes sure it is the last thing on screen) and the
   reminder: adopt devices in the app before the deploy telemetry gate
   can pass again.

This script is the sanctioned exception to the deployment-discipline rule
that spine data services are never recreated; CLAUDE.md gets one line
saying so and pointing here.

## Testing

- **Backend (TDD)**: unit tests for code generation (alphabet, grouping,
  case/dash normalization), hash-and-rotate, throttle behavior, and the
  three `POST /pair` outcomes (valid-in-setup, invalid-in-setup,
  code-outside-setup). Contract tests for the 3.7.0 additions. Migration
  test covering the backfill. Integration tests run against loopback dev
  containers only, per the environment boundary — never the hub.
- **iOS**: contract re-pin (compile failure = drift caught), kit tests
  for any extracted logic (code normalization mirror), UI states per
  §7.1. The full first-pair path is bench-verified, not CI-run.
- **Factory reset**: manual acceptance on the hub, documented in the
  script header — its dry run IS the 2026-08-14 transcript.
- **Feature acceptance**: repeat the 2026-08-14 clean-install walkthrough
  end to end with zero unexpected SSH: `factory-reset-pi.sh` → deploy
  prints code → erase sim → fresh install → enter code → paired → adopt
  → telemetry verified on the wire.

## Out of scope

- Any pairing-window or setup control in the iOS app or the API beyond
  the one code-gated pair call. The authorization boundary for opening
  pairing stays on the hub.
- Re-entering setup mode by revoking all clients (fire-escape covers it).
- Web UI (separate spec, 2026-08-14, held).
- Persistent throttle state or per-IP tracking.

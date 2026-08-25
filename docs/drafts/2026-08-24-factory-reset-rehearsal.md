# Factory-reset rehearsal — 2026-08-24

Expectations authored **before** the run (13:50 PDT), per David's directive:
define what the script is expected to do first, then run it and record what it
actually did. Actuals are appended under each expectation as the run proceeds;
an empty "Actual" is a checkpoint not yet reached, never one assumed passed.

## Purpose

Rehearse the full net-new recovery path — wipe → redeploy → re-pair →
re-import → re-adopt → cold boot — while nothing is at stake, so the first
real use of `scripts/factory-reset-pi.sh` is not also its first full test.
The script itself ran live once (2026-08-15, which surfaced the `--help`
fall-through and the double-setup-code defects, both fixed); the *whole
journey* from wipe to verified telemetry and a working app has not been run
end-to-end since the clock oracle, audit integrity (#67), schedules (#60),
and heartbeat chain (#65) landed.

## Preconditions (verified 13:47 PDT, before anything ran)

| Check | State |
|---|---|
| Local main = origin/main = Pi checkout | `7103c80` all three, clean tree |
| Containers | all six healthy (app 19 h, spine longer) |
| Prior backup | `/home/david/backups/bellasreef-pre-rehearsal-20260823-181759.tar.gz` (30,113 B, 08-23 18:18; schema 0021 / contracts 4.2.0 verified at creation) |
| Import manifest | `/etc/bellasreef/devices.import.yaml` refreshed 08-23: **2 actuators** (`pca9685-0` "Meter Check", `pi-pwm-0` "Light 1") **+ 1 sensor** (`ds18b20-28-000000bfe244`). The stale 08-11 copy (probe only) is preserved as `.bak-2026-08-23`. |
| iOS app | paired (sim); full walkthrough passed 08-23 |

## Run protocol

- Full stdout/stderr of the script tee'd to a transcript file; relevant
  excerpts land in this document, the whole transcript kept with the run.
- The confirmation word is written by David, verbatim, with the destruction
  notice on screen — relayed through the script's documented piped form
  (`ssh -n` exists precisely so that form works). Claude never authors the
  word on its own.
- `docs/` additions are stashed (`git stash -u`) around the run because the
  embedded `deploy-pi.sh` refuses a dirty tree, and restored immediately
  after.

## Expected behavior — the script, phase by phase

### Phase 1 — mandatory pre-reset backup

Expected: `bellasreef backup --out /backups/bellasreef-pre-factory-<stamp>.tar.gz`
runs **before** the consent prompt and succeeds; a failure aborts with nothing
touched. (An aborted-at-consent run still leaves this backup — additive,
harmless, by spec.)

Actual: **PASS.** `/backups/bellasreef-pre-factory-20260824-154825.tar.gz`
written (30,217 bytes), self-reporting schema `0021`, contracts `4.2.0`,
taken `2026-08-24T22:48:27Z` — before the consent prompt, as specified. The
backup's own credential warning printed with it.

### Phase 2 — consent

Expected: destruction notice listing the three volumes and what they mean
(pairings, devices, audit log, telemetry); only the literal word
`factory-reset` proceeds; anything else dies "not confirmed; nothing touched".

Actual: **PASS.** Destruction notice printed with the three volumes and the
fresh backup path. David typed the word at 15:47 PDT with the notice on
screen (relayed verbatim through the documented piped form); accepted.

### Phase 3 — stop, down, wipe

Expected, in order (each gate dies without proceeding on failure):
1. `sudo systemctl stop bellasreef.service`
2. `docker compose down` (a stopped-but-not-removed container still holds its
   volume — measured 2026-08-14, hence the ordering)
3. `docker volume rm bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data`
   — all three removed, none "in use".

Actual: **PASS.** All three steps ran in order with no errors; volume
removal reported all three names, none held.

### Phase 4 — redeploy from zero

Expected: `deploy-pi.sh --host <pi> --no-verify --no-setup-code` walks its
normal ladder — tree deployable, contract version read, **CI green for
`7103c80`**, Pi checkout reset, ghcr credential, images pulled by SHA tag
`7103c80f` with digests recorded, **migrations applied: alembic builds schema
`0021` from an empty database**, boot unit pointed/installed, discovery
record, app containers started, boot unit enabled + started, container
status, API answers speaking contracts **4.2.0**. The telemetry-verify leg is
skipped *by construction* (empty registry ⇒ nothing on the wire), and **no
setup code is printed by this phase** — that is phase 6's job alone.

Actual: **PASS.** Every rung observed in order; images pulled at SHA tag
`7103c80f`, digests recorded; alembic ran the full chain on the empty
database ending `0020 -> 0021`; `up --wait` reported all six containers
Healthy; the skip printed itself explicitly ("deploy: skipping wire
verification at your request") rather than passing silently; no setup code
appeared in this phase's output.

### Phase 5 — factory-fresh assertions (script dies on any miss)

| Assertion | Expected |
|---|---|
| `GET /api/v1/info` | answers ≤30 s; `paired_client_count=0`; `setup_mode=true` |
| Postgres | `devices` count 0; `audit_log` count 0 |
| Alembic | `alembic current` reports head (`0021`) |
| JetStream | hardware-io logs ≥7 `stream created` lines ≤60 s (BR_CMD, BR_STATE, BR_REGISTRY, BR_CAPABILITY, BR_CHIP, BR_ASSIGNMENT, BR_AUDIT) — *created*, never *updated*, against a fresh volume |
| Capabilities | ≥1 `capability announced` ≤60 s. Full expectation on this hardware: RP1 PWM channels per what `pinctrl` proves muxed, 16 PCA9685 channels (board present on bus 1 as of 08-15), and the 1-Wire probe |

Actual: **PASS, all five.** `0 paired clients, setup mode open` · `0
devices, 0 audit_log rows` · `alembic at head` · `all 7 JetStream streams
recreated` · 3 capability lines at `22:48:58Z`, breakdown exactly as
expected: `pi-pwm` channels=4, `w1-bus` channels=1, `pca9685` channels=16.

### Phase 6 — mint the final setup code

Expected: exactly **one** setup code on screen, printed last (phase 4's
`--no-setup-code` exists so no earlier code was minted and rotated dead).
Codes rotate on mint and only the hash is stored — this code is the only
pairing credential in existence.

Actual: **PASS.** Exactly one code in the entire transcript, printed as the
final output (not reproduced here — it is a live pairing credential,
consumed by checkpoint A). Script exited 0. Whole run, backup to minted
code: **~90 seconds** (15:48:25 → 15:49:5x PDT).

## Expected behavior — the journey after the script

### Checkpoint A — re-pair the app

David pairs the iOS app using the phase-6 code. Expected: pairing succeeds;
`/api/v1/info` flips to `paired_client_count=1`, `setup_mode=false`.

Actual: **PASS.** Sim paired at ~15:52 PDT; `/api/v1/info` read
`paired_client_count=1`, `setup_mode=false`, `pairing_open=false`,
contracts `4.2.0` (verified 15:52 PDT).

### Checkpoint B — re-import devices

The documented dance: mint a seed CLI token via the paired path, run
`docker compose exec api bellasreef devices import
/etc/bellasreef/devices.import.yaml`, revoke the seed token. Expected: 3
devices land under their **original ids** (same NATS subject tokens, same
history keys — an id change forks a device, the manifest header says so);
DS18B20 readings resume on the wire at the manifest's 5 s cadence;
actuator safe states hold (both lights dark, duty 0).

Actual: **PASS, with findings.** Window opened (`bellasreef pair`, 300 s
TTL), `seed-import-cli` paired through the window (HTTP 200, refresh token →
access token via `POST /api/v1/token`), import bound **3/3 created** under
the original ids, client revoked ("refresh token dead, row stays revoked").

Findings, in the order hit:

- **F1 — the documented import command does not work as written.**
  `docker compose exec api bellasreef devices import
  /etc/bellasreef/devices.import.yaml` (CLAUDE.md, deployment discipline)
  fails: that path exists on the *host* and is not mounted into the api
  container. Worked around by `cat`-ing the manifest into the container's
  `/tmp` first. Fix is either a compose mount for `/etc/bellasreef` (ro) or
  correcting the documented command to the copy-in form.
  RESOLVED 2026-08-25: `/etc/bellasreef` is now a read-only mount in the api
  service (deploy/compose.yaml), so the documented command works as written.
- **F2 — `/info.paired_client_count` counts clients *ever*, revoked
  included** (`total_clients_ever`; deliberate — TOFU keys on it so revoking
  everything cannot reopen first-use pairing). After the seed dance it reads
  2 with 1 live client. The *semantics* are correct and load-bearing; the
  *name* reads as "currently paired", and any UI surfacing it inherits the
  confusion. Check what the System tab's Access row shows.
- **F3 (iOS, from David's screenshots at 15:53) — "Waiting for a sensor"
  assumes a temp probe exists and is wanted.** `TankMonitor.statusLine`
  returns it whenever `probes.isEmpty` on a live connection — truthful
  during the empty-registry window, but a lighting-only hub would sit amber
  forever. Direction: key on *adopted sensors*, not the probe stream.
- **F4 (iOS, same screenshots) — the sensor-centric banner leaks into the
  Lighting tab** (`LightingView` reuses `monitor.tone`/`statusLine`), where
  "Waiting for a sensor" is irrelevant to what the tab shows.

F1 is a docs/compose fix; F2 an API-naming observation; F3/F4 join the iOS
UX backlog. None block the rehearsal.

- **F5 (iOS, David at checkpoint E, 16:00 PDT) — 1-based channel display
  contradicts every other voice in the system.** The Devices row for
  `pca9685-0` reads "pca9685 · ch 1": `ChannelLabel.humanNumber` shifts
  0-based hub channels up by one, deliberately, with an explanatory footer
  that exists only on the Hardware adopt screen. Device ids, audit rows,
  backend logs, bench notes and board silkscreen all speak 0-based, so the
  app's is the only 1-based voice — and it confused the operator during this
  rehearsal, at a bench where numbers place meter probes. Recommendation:
  delete `humanNumber`, show the hub's channel verbatim (the ROM-string rule
  already on the books, applied to numbers). **RULED (David, 16:02 PDT):
  0-based verbatim — delete `humanNumber`, show the hub's channel string
  as sent. iOS change queued behind the rehearsal.**

### Checkpoint C — the deploy telemetry gate closes the loop

Run the full `scripts/deploy-pi.sh` (no `--no-verify`). Expected: it passes
end-to-end **including fresh telemetry on the wire** — the same gate that
cannot pass on an empty registry now can, which is the "net-new deploy"
half of the rehearsal's name.

Actual: **PASS** (15:57 PDT). Full ladder green, ending
`✓ deployed 7103c80f — API answering at contracts 4.2.0, fresh sample on
the wire (28.25)`. Two footnotes: the deploy prints "2 paired client(s)" —
F2's ever-count surfacing in operator output — and the success line has a
stray `]` in `(28.25])` (one-character cosmetic in deploy-pi.sh).

### Checkpoint D — audit rows carry names and actors (#67 live)

Expected: the audit log rows produced by A–C show resolved device display
names and real actors (paired client / CLI token identity), not raw ids or
blanks — the #67 acceptance, observed on a from-zero database. Also expected,
recorded here as ruled today: any unknown-category event parks in
`'safety'` with the original preserved in `original_category`
(`audit_writer.py`; David's ruling 2026-08-24 confirms shipped behavior —
no code change).

Actual: **PASS** (verified 15:58 PDT, 11 rows). Event-time stamps match
payloads; CLI rows carry real operator identity
(`bellasreef@<container>`), API rows name the client (`iPhone 9026`,
`seed-import-cli`); all three `device.bound` rows carry their device_id and
the binding actor; categories all inside the enum (`auth`, `config`);
immutability trigger and category CHECK (with `'safety'` in the enum)
present in the live schema. Unprompted bonus: the first post-wipe row is
`token.rejected` — the sim presenting its pre-wipe refresh token and being
refused before re-pairing, proving old credentials die with the wipe.
Observation (minor): `device.bound` actor is the client's bare UUID, not
its resolved name.

### Checkpoint E — ghost warning on re-adopt

A wipe destroys schedules too, so the ghost must be *recreated* to be tested:
create a schedule in the app, assign it to `pca9685-0`, **forget** the device
(assignments survive forget by design, spec 2026-08-19), then re-adopt from
the adopt sheet. Expected: the adopt confirmation dialog names the schedule
("…is still assigned to this channel and resumes immediately").

Known risk, declared up front: forget-on-FK-RESTRICT can 500 (open bug, memory
`alert-episode-latch-and-forget-500`). If forget 500s here, that is the known
open bug surfacing on schedule — record it, do not chase it mid-rehearsal.

Actual: **mechanism PASS; two iOS findings; dialog re-run below.**
Sequence (audit + engine logs + screenshots): schedule created 15:59,
assigned to `pca9685-0`; `device.unbound` 16:02:53 via **Unadopt** (no
500 — note the known FK bug guards *forget/Clear*, which was not used);
the schedule editor surfaced the ghost ("Still assigned · Meter Check ·
Not adopted — output resumes if this channel is adopted again") and the
Devices screen showed a **Detached** row ("released — history kept",
Re-add/Clear). David re-adopted via **Re-add** at 16:03:50; the engine
slewed from dark (`lighting:converge` at 0.05/s) and arrived at
`lighting:ramp` duty **0.6** — the curve's value for that hour — within
16 s. Assignment-survives-removal and output-resumes are both proven on
the wire.

- **F6 — Re-add bypasses the resumes-immediately warning.** The adopt
  sheet's confirmation dialog (the ghost warning) guards only the
  Hardware-screen path; `SystemView`'s Re-add button calls `readopt()`
  bare. A Detached row is precisely where a surviving assignment is most
  likely, and output resumed at 60 % with no confirmation. Fix: give
  Re-add the same ghost-aware confirmation the adopt sheet has.
- **F7 — the schedule editor's ghost section is stale after re-add.** A
  minute after `device.bound`, with the engine already commanding 0.6,
  the editor still showed "Not adopted — output resumes…" while the
  Devices screen had updated. The editor's catalog snapshot does not
  refresh on this transition.

The checkpoint's named artifact — the adopt-sheet dialog naming the
schedule — was bypassed by the Re-add route; re-run via unadopt →
Hardware adopt sheet:

**Re-run (16:11–16:13 PDT): PASS.** Unadopt dropped the channel to safe 0
(safety event + `actuators:1` rebuild verified on the hub). Adopting the
channel through the Hardware adopt sheet fired the confirmation dialog
with the ghost warning, verbatim: *"Adopting starts real output on this
channel as soon as the engine's schedule runs. "Brining" is still
assigned to this channel and resumes immediately. Only adopt hardware you
have bench-verified."* — the schedule named, the resume declared. Also
observed live on the Hardware leaf: the chip-state surface reporting
"initialised · 502.7 Hz · INVRT off · 16 channels" on the post-wipe
chip. After the adopt (16:14 PDT): engine resumed `lighting:converge` →
`lighting:ramp` duty 0.6 within 15 s of `device.bound` — the resume
promise proven on the wire a second time. Also settled: the app's Access
row shows the *active* client count, so F2 stays an API-field-name issue
only.

- **F8 — re-claiming a detached row silently renames it.** The adopt
  sheet prefilled the generic "Light 1" while re-claiming the row named
  "Meter Check"; submitting overwrote the name, leaving two devices both
  called "Light 1" (identity, history and assignment were correctly
  preserved — only the display name was clobbered). Fix: when the channel
  matches a detached registry row, prefill that row's existing name.
  Consequence, seen immediately (16:14): the schedule editor's
  assigned-lights picker lists bare display names, so the two "Light 1"
  rows are indistinguishable — the operator cannot tell which silicon
  they are assigning a schedule to. Minimal fix: picker rows get the
  `DeviceSubtitle` (driver · channel) the Devices screen already has.
  Whether display names should be *unique-enforced* at bind time is a
  David ruling for the naming pass, post-rehearsal.

### Checkpoint F — cold boot drills the live clock-oracle path

Reboot the Pi. Expected: fake-hwclock restores time pre-network, chrony steps
it, `time-sync.target` means synchronized; the stack comes up under the one
boot unit; **containers decide clock trust via the adjtimex kernel oracle**
(no `BELLASREEF_ASSUME_CLOCK_TRUSTED` anywhere on the hub since #70); engine
resumes the schedule and slews from dark (#68's recovery shape); no service
treats the clock as trusted before the oracle says so.

Actual: **PASS, all assertions** (power pulled and reconnected 16:16 PDT —
a true power-loss boot, not a reboot). SSH answered 41 s from power-on;
all six containers healthy ~60 s in under the one boot unit, untouched.
`NTPSynchronized=yes` and `time-sync.target` active before the app
containers finished health checks. hardware-io logged `starting
clock_trusted=true` at 23:17:21Z and asserted safe state *before* building
devices; control-engine `starting clock_trusted=true` at 23:17:23Z — the
kernel oracle granting trust on a genuine RTC-less cold boot, no
`BELLASREEF_ASSUME_CLOCK_TRUSTED` in existence on the hub. The engine then
published `lighting:initial duty 0.0` and slewed from dark at 0.05/s to
the curve, arriving at 0.6 on "Brining" unprompted — #68's recovery shape
from a real blackout. Wire fresh on both kinds: `pca9685-0 = 0.6`,
DS18B20 28.062 °C at 0 s age. Power loss to schedule-restored: under two
minutes, fully automatic.

## Outcome

**Rehearsal complete, 16:19 PDT. Every phase and every checkpoint passed:**
script phases 1–6 (backup → consent → wipe → redeploy → assertions → one
setup code, ~90 s), checkpoints A (re-pair), B (import 3/3 under original
ids), C (full deploy with the telemetry gate green), D (audit integrity on
a from-zero database), E (ghost warning naming the schedule on the adopt
sheet; resume proven on the wire three separate times), F (cold boot:
oracle-granted clock trust, safe-state-first startup, automatic slew back
to the curve in under two minutes).

**Rulings closed today:** audit unknown-category fallback = `'safety'`
(shipped code already matched; no change). Channel display = 0-based
verbatim (F5).

**Findings, none blocking, all filed above:** F1 import-command path not
mounted (docs/compose fix) · F2 `paired_client_count` counts clients ever
(API naming; no iOS surface affected) · F3 "Waiting for a sensor" assumes
a probe is wanted (iOS) · F4 sensor banner leaks into Lighting tab (iOS) ·
F5 1-based channel display, ruled 0-based (iOS) · F6 Re-add bypasses the
resumes-immediately warning (iOS) · F7 schedule editor ghost section
stale after re-add (iOS) · F8 re-claim silently renames the device and
duplicate names make the assignment picker ambiguous (iOS + a uniqueness
ruling pending). Plus one one-character cosmetic: `(28.25])` in
deploy-pi.sh's success line.

**Residue on the hub, deliberate:** `pca9685-0` is currently named
"Light 1" (F8's clobber, left in place as evidence); rename at will.
Backups `pre-rehearsal-20260823` and `pre-factory-20260824` both on the
hub. The rehearsal transcripts (reset + verify deploy) are kept beside
this document.

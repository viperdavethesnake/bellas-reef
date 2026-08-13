# Backup and restore

PRD R14. One command writes a restorable archive. One command loads it onto new
hardware, or refuses and tells you why.

The archive does not contain everything, and that is deliberate. What it leaves
out is written into the manifest and printed every time you run either command,
so you never have to guess whether missing data was excluded or lost.

## What is in the archive

A gzipped tar with exactly two members.

| Member | What it is |
|---|---|
| `manifest.json` | schema revision, contracts version, which hub this came from, sha256 and size of the dump, the telemetry snapshot name, the `contains` list, and the omissions list |
| `postgres.dump` | `pg_dump --format=custom --no-owner --no-privileges` of the whole database |

Ownership and grants are stripped on purpose. They are facts about a
deployment, not about your tank, and carrying them makes a restore fail on new
hardware for reasons that have nothing to do with the reef.

The Postgres dump is the valuable part, because almost everything that makes
the hub *yours* lives there: device names, alert thresholds, calibration, the
dosing journal, the audit log, paired clients, and the JWT signing key. That
last one is why a restored hub still recognises your phone. Restore the
database and existing sessions keep working.

## Handle the archive like a password-manager export

Read the paragraph above again, because it has a second meaning. The signing
key is what makes a restored hub recognise your phone — and it is in the file,
in plaintext, along with the id of every client that has ever paired.

**Anyone who can read a backup archive can mint a valid access token for any
client of the hub it came from.** Not "could eventually brute-force". Mint. The
archive is not a copy of your settings; it is a key to your tank.

It is not encrypted, and that is a decision rather than an oversight. This is a
home hub, the archive is a file on your own machine, and an encrypted backup
needs a key you would then have to back up somewhere — which for most people
means a passphrase written down next to the thing it protects, or a backup
nobody can open when they finally need it. So the archive is treated the way you
would treat a password-manager export:

- Written `0600`, owner-only, from the instant the file exists. It never passes
  through a world-readable state, and overwriting an older archive tightens its
  mode rather than inheriting it.
- Keep it off shared storage, out of chat, and off anything with a public link.
- There is **no signing-key rotation**, so a leaked archive cannot be cleaned up
  after the fact. Revoking clients does not help: the key that signs their
  replacements is the same key that is in the file.

`bellasreef backup` and `bellasreef restore` both print this at the end of every
run, above the omissions list, so you meet it while you still have the file in
front of you. The manifest carries the same text in its `contains` list, next to
the `omissions` list, so the file also explains itself to whoever finds it
later:

```bash
tar xzOf backup.tar.gz manifest.json | jq .contains
```

If you copy an archive off the hub, check the mode survived the trip — `scp`
preserves it, a drag into a cloud folder does not.

## Requirements

`pg_dump` and `pg_restore` must be available, and their major version must be
at least the server's. An older `pg_dump` refuses to dump a newer server, which
is the failure you are most likely to hit first.

They are found on `PATH`, or in the directory named by `BELLASREEF_PG_BIN`.
That second option is not decoration. Homebrew's `libpq` is keg-only, so on a
Mac the tools are installed and `PATH` does not have them:

```bash
export BELLASREEF_PG_BIN=/usr/local/opt/libpq/bin   # or /opt/homebrew/... on Apple silicon
```

On the hub, Postgres runs in a container but the CLI runs on the host, so the
host needs its own client package — `sudo apt-get install -y
postgresql-client-17`. Host-setup §11 is the authority on which package, why
the container's own copy cannot be borrowed, and what happens when the server
major moves.

## Taking a backup

On the hub, the CLI runs from the deployed clone the same way `bellasreef
revoke` does (host-setup §10): source the service environment, then run the
installed script. `api.env` must carry `BELLASREEF_VM_URL` as well as the DSN
— see the variable table in host-setup §7.

```bash
cd /home/david/bellasreef
set -a; . /etc/bellasreef/api.env; set +a
.venv/bin/bellasreef backup --out ~/backups/bellasreef-$(date +%Y%m%d-%H%M%S).tar.gz
```

`.venv/bin/bellasreef`, not `uv run bellasreef`: the script form runs what the
last deploy synced, while a bare `uv run` may re-resolve and rewrite the venv
the live services are executing from.

From a workstation there is no direct path: the spine's ports are
loopback-only on the hub since the 2026-08-12 cutover, and `bellasreef.local:5432`
refuses connections *by design* — do not "fix" that by re-exposing the ports.
Either run the command over ssh as above and `scp` the archive off, or tunnel:

```bash
ssh -L 5432:localhost:5432 -L 8428:localhost:8428 bellasreef.local
# then, in another shell, with the password from the hub's api.env:
export BELLASREEF_DATABASE_URL="postgresql+asyncpg://bellasreef:***@localhost:5432/bellasreef"
export BELLASREEF_VM_URL="http://localhost:8428"
bellasreef backup
```

With no `--out`, the file lands in the working directory as
`bellasreef-<host>-<timestamp>.tar.gz`.

### The telemetry snapshot is not optional by accident

Backup calls VictoriaMetrics `/snapshot/create` and records the snapshot name.
If no VM URL is available it refuses to run unless you pass
`--no-telemetry-snapshot`.

That friction is the point. A hub whose history quietly stopped being captured
is the exact failure this command exists to prevent, so skipping telemetry has
to be something a person chose, not something an unset variable decided.

### Where the snapshot actually lives

`/snapshot/create` gives you a consistent, hardlinked view inside the VM data
volume. It is not a portable file, and the backup process has no access to that
volume, so the archive records where the snapshot is instead of pretending to
carry it.

To capture the bytes, copy them out on the host:

```bash
docker run --rm -v bellasreef_vm-data:/storage -v "$PWD":/out alpine \
  tar czf /out/vm-<snapshot>.tar.gz -C /storage/snapshots <snapshot>
```

Snapshots accumulate. Delete one you have copied:

```bash
curl -X POST "http://localhost:8428/snapshot/delete?snapshot=<snapshot>"
```

## Restoring onto fresh hardware

The order matters, and one step is easy to get wrong.

**1. Prepare the host.** Follow `docs/host-setup.md`. Overlays, chrony, avahi
and the systemd units are host configuration, and none of it is in the archive.

**2. Recreate the two host-state files** — neither is in the archive, on
purpose, so that a file you copy to a laptop is not also a credential:

- `deploy/.env` from `deploy/.env.example` — the database password and the
  `i2c`/`gpio` group IDs (host-setup §1b).
- `/etc/bellasreef/<service>.env` — the service environment, including the
  DSN the restore command itself will read. Variable-by-variable recipe in
  host-setup §7; the password must match what you put in `deploy/.env`.

**3. Start the spine.** The unit files were installed in step 1 (host-setup
§7), so the spine comes up supervised rather than as hand-run containers:

```bash
sudo systemctl start bellasreef-spine.service
```

NATS and VictoriaMetrics idle harmlessly; what this step is for is Postgres.

**4. Create an empty database. Do not migrate it.**

This is the step people get wrong. The dump carries the entire schema,
including the `alembic_version` stamp. Running `alembic upgrade head` first
gives you a database full of empty tables, and restore will refuse it with
`target-not-empty`. Which is correct behaviour, but it is easier to just not
migrate.

**5. Restore.** From the clone on the new host, with `postgresql-client-17`
installed (host-setup §11), the venv synced (`uv sync --frozen` — deploy-pi.sh
has not run yet on a machine being restored), and the DSN pointing at the
empty database:

```bash
cd /home/david/bellasreef
set -a; . /etc/bellasreef/api.env; set +a
.venv/bin/bellasreef restore /home/david/backups/bellasreef-<timestamp>.tar.gz
```

**6. Start the app units.**

```bash
sudo systemctl start bellasreef-hardware-io bellasreef-control-engine bellasreef-api
```

hardware-io re-announces its devices on boot, so the spine rebuilds itself.
Your paired phones keep working, because the signing key came back with the
database.

**7. Restore telemetry if you captured it.** Stop VictoriaMetrics, unpack the
snapshot tarball into the `vm-data` volume, start it again. Telemetry is
history, not control state, so the tank runs fine without this step.

## When restore refuses

Nothing is written to the target database until the archive has been verified
whole. Digest, size, manifest shape, manifest version, schema revision and
target emptiness are all checked first. The load itself then runs inside
`--single-transaction --exit-on-error`, so Postgres makes the final step
all-or-nothing too. There is no sequence of failures that leaves you with a
half-populated database reporting success.

Every refusal prints a stable reason slug. Match on the slug, not the sentence.

| Reason | What happened | What to do |
|---|---|---|
| `archive-missing` | no file at that path | check the path |
| `archive-unreadable` | not a valid gzip/tar, or truncated | the copy is damaged, get another |
| `manifest-missing` | no `manifest.json` inside | not one of our archives |
| `manifest-unreadable` | manifest is not valid JSON | archive is damaged |
| `manifest-incomplete` | a required field is absent or the wrong type | archive is damaged |
| `manifest-version-unsupported` | written by a hub using a newer archive layout | restore with a newer build |
| `payload-missing` | no `postgres.dump` inside | archive is damaged |
| `payload-corrupt` | the dump does not match its recorded sha256 or size | archive is damaged, do not use it |
| `schema-revision-unknown` | taken at a migration this build has never heard of | restore with a build at least as new as the one that wrote it |
| `target-not-empty` | the database already has tables | use an empty database, or pass `--force` to drop and replace |
| `pg-restore-failed` | `pg_restore` errored, transaction rolled back | read the message, target is unchanged |

`schema-revision-unknown` is the one worth understanding. An archive from a
newer hub describes tables and constraints this code does not know about.
Loading it would give you a database the running services cannot describe, and
worse, one that looks restored.

## What is not covered

Printed by both commands, and in `manifest.json` with the reason and the
recovery for each.

**Telemetry samples.** A snapshot is taken and named, but its bytes stay in the
VM volume. Copy them out with the command above.

**NATS JetStream state.** Streams, durable consumers and queued commands are
excluded, and this one is a safety decision rather than a convenience. BR_CMD
holds actuator commands. Restoring a stale one would replay actuation against a
tank nobody has looked at yet. Registrations come back on their own when
hardware-io announces its devices at boot.

**Deployment secrets.** `deploy/.env` holds the database password and host
group IDs. Recreate from the example file.

**Host configuration.** Boot overlays, chrony, avahi, systemd units. All in
`docs/host-setup.md`, which is the only host-touching surface this project
allows itself.

**Container images.** Pinned by digest in `deploy/compose.yaml`. `docker
compose pull`.

## Which hub an archive came from

`manifest.hub.hub_id` is the answer, and it is the only one that survives being
moved around. It is a UUID written to `hub_identity` once at first boot and
carried through a restore with the rest of the data, so an archive names the hub
rather than the circumstances of its own creation.

The other three fields corroborate and none of them is sufficient alone.
`database_host` is Postgres exactly as the DSN addressed it — a loopback name
(`localhost`, `127.0.0.1`, a tunnel) identifies nothing, a network name like
`bellasreef.local` identifies everything. `taken_on` is the machine that ran
the tool and has the mirror-image problem. `database` is almost always
`bellasreef`.

One case leaves `hub_id` null: a database that has been migrated but has never
had a service start against it, because the id is minted at startup rather than
in the migration. Stamping it in the migration would give a hub about to have a
backup restored into it a brand-new identity, destroying the fact the row exists
to carry. A manifest without a `hub_id` is old, not wrong — it reads, and it
falls back to the three corroborating fields.

## Verifying a backup without restoring it

```bash
tar xzOf backup.tar.gz manifest.json | jq .
```

Check `schema_revision` against what your build knows
(`bellasreef_db.revisions.KNOWN_REVISIONS`), and confirm `postgres.sha256`
matches:

```bash
tar xzOf backup.tar.gz postgres.dump | shasum -a 256
```

A backup you have never restored is a guess. Restoring into a scratch database
now and then is the only way to know, which is what
`services/api/tests/test_backup_restore.py` does on every CI run: it restores
into a database created moments earlier, then makes a client that paired with
the old hub present its refresh token to the new one and mint a working token.

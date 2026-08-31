# Bella's Reef hub

This is what a Bella's Reef hub runs from. It's generated from the
development repository on each release and is never edited by hand; if
you're looking at a diff here, it belongs upstream. The release this
checkout is pinned to is recorded in `deploy/release.env`.

## What you need

- A Raspberry Pi 5, or another arm64/amd64 Linux 6.x board. See
  `docs/hub-platform-requirements.md` for what qualifies.
- A 64-bit OS.
- 16 GB of storage as a practical minimum.
- Network reachable from your phone.
- A phone with the Bella's Reef app.

Memory: the installer warns if it finds less than 2 GB. We're still
collecting real-world numbers below that line, so treat the warning as
real rather than as a hard wall.

## Install

```bash
sudo apt install -y git   # only if git is missing
git clone https://github.com/viperdavethesnake/bellasreef-hub.git ~/bellasreef
cd ~/bellasreef
./scripts/install-hub.sh
```

The installer runs in six phases:

1. **Already deployed?** Exits if this machine already looks like a hub.
2. **Requirements.** Checks Docker, the clock, and mDNS. A failure here
   stops the run.
3. **Hardware.** Takes inventory of what this machine can control.
4. **Configuration.** Writes `deploy/.env`.
5. **Deploy.** Pulls images, runs migrations, installs the boot unit.
6. **Verify.** Confirms the stack came up and prints your pairing setup
   code.

Along the way it offers to install Docker, add you to the `docker` group,
publish avahi's `_bellasreef._tcp` service record and interface allowlist,
and set up Docker log rotation. It asks before every one of these; nothing
happens without your say-so.

One of those, the docker group, needs a fresh login to take effect. If the
installer adds you to the group, log out and back in (reconnect over ssh),
then run `./scripts/install-hub.sh` again.

## While the images are private

Bella's Reef container images are on GitHub Container Registry and, for
now, private. Before the last command above, log in:

```bash
docker login ghcr.io -u <github-username>
```

For the password, use a personal access token with `read:packages` scope,
not your GitHub password. This section goes away once the packages are
public.

## After the install

Phase 6 prints a setup code. Open the app on your phone, pick this hub,
and enter the code to pair.

A fresh hub has no devices to read or show. To tell it what's attached,
copy the example device file and edit it for your hardware:

```bash
sudo cp deploy/config/devices.yaml.example /etc/bellasreef/devices.import.yaml
```

Then, with an access token from a paired client (docs/host-setup.md section
10), run the import command the installer printed at the end of setup:

```bash
cd ~/bellasreef
docker compose -f deploy/compose.yaml --env-file deploy/.env \
  exec api bellasreef devices import /etc/bellasreef/devices.import.yaml --token <access token>
```

## Later

`scripts/update-hub.sh` moves a hub to a newer release: it picks the
newest stable `v*` tag (`--pre` for the newest pre-release, `--ref` to pin
one, never `main`, never a downgrade on a plain run), checks it out and
re-execs itself, takes a mandatory backup, pulls/migrates/recreates the
three app services only, and verifies — PASS (fresh telemetry on the
wire), NO DEVICES (empty registry; complete; adopt from the app), or FAIL.

`scripts/factory-reset-hub.sh` wipes the hub back to nothing: every
pairing, every device, the audit log, all telemetry history. It takes a
backup before it destroys anything, and it will not proceed until you
type `factory-reset` at the prompt.

`scripts/install-hub.sh --uninstall` removes the stack instead of resetting
it: the data volumes (once you type `uninstall`), the boot unit, and
`deploy/.env`. It keeps your backups, `/etc/bellasreef`, the host
configuration the installer set up, the pulled images, and this checkout.
It also cleans up after a failed or partial install, whatever state it was
left in.

## If you get locked out

If you can't authenticate to the API anymore, `bellasreef pair` and
`bellasreef revoke` talk to Postgres directly instead of going through the
API, so they still work. Full walkthrough in
[`docs/host-setup.md`](docs/host-setup.md#10-getting-back-in-bellasreef-pair-and-bellasreef-revoke).

```bash
cd ~/bellasreef
alias br='docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef'

br pair --ttl 600     # open a 10-minute window; pair a replacement device
br revoke --list      # every client this hub has ever paired
br revoke <id>        # turn one off, by id or by unambiguous name
```

Replacing a phone is both commands, in order: pair the new one, then
revoke the old one. A pairing window only adds a client; it never removes
one, so there's no "reset and start over" shortcut here.

## Docs

- [`docs/host-setup.md`](docs/host-setup.md): the host mutations the
  installer makes or offers, and how to do them by hand.
- [`docs/hub-platform-requirements.md`](docs/hub-platform-requirements.md):
  what a board needs to run a hub at all.
- [`docs/backup-restore.md`](docs/backup-restore.md): backing up and
  restoring the data volumes.

## Licence

AGPL-3.0-only. See [`LICENSE`](LICENSE). Source at
[`github.com/viperdavethesnake/bellas-reef`](https://github.com/viperdavethesnake/bellas-reef).

# Hub prerequisites

What a fresh host needs before `scripts/deploy-pi.sh` can run against it.
Follow it top to bottom on a machine that has never held the stack, or on one
that has been deliberately wiped back to bare packages. Everything here is
host state; nothing here is Bella's Reef code.

`docs/host-setup.md` owns the host mutations that are not about the runtime
(device tree overlays, clock, avahi, headless stripping). Do those first if
the hardware is new. This doc owns the runtime itself: Docker, the repo
clone, the deploy secrets, and the one credential that only developers need.

Written 2026-08-25 for the clean-slate rebuild. The reference host is the
Pi 5 described in CLAUDE.md's "Verified host facts" (Debian 13 trixie,
arm64, kernel 6.18); commands below were run there. `docs/hub-platform-
requirements.md` says what class of machine qualifies at all.

## 1. Docker Engine with Compose v2

The whole stack runs as containers under one boot unit, so Docker with the
Compose v2 plugin is the deployment model, not a convenience. Version floor:
whatever Docker's own repository currently ships (the reference host runs
29.x). Debian's `docker.io` package is not the install path.

Install with Docker's convenience script, which adds Docker's apt repository
(`/etc/apt/sources.list.d/docker.list`, keyring at
`/etc/apt/keyrings/docker.asc`) and installs `docker-ce`, `docker-ce-cli`,
`containerd.io`, `docker-buildx-plugin`, and `docker-compose-plugin`:

```bash
curl -fsSL https://get.docker.com | sudo sh
```

This is the same path `scripts/install-hub.sh` offers when it finds no
Docker. If piping a downloaded script to a root shell is unacceptable in
your setting, Docker documents the manual repository setup at
https://docs.docker.com/engine/install/debian/ and the result is identical.

Then put the operating user in the `docker` group. Services must not run as
root, and neither should deploys:

```bash
sudo usermod -aG docker $USER
```

**Group membership is granted at login.** The current shell does not have
it; log out and back in (for ssh, reconnect). `install-hub.sh` fails
its Docker check on exactly this: docker installed, user in the group,
daemon still unreachable, because the session predates the grant.

Reconnecting has a trap of its own: ssh connection multiplexing
(`ControlMaster`/`ControlPersist` in the client's `~/.ssh/config`) silently
reuses the old connection, which is the old login, which does not have the
group. Found live on the 2026-08-25 rebuild: `docker info` still read
permission-denied after "reconnecting", until the master connection was
bypassed. If the denial survives a reconnect, force a genuinely new
connection (`ssh -o ControlPath=none <pi-host>` or `ssh -O exit <pi-host>`
first) before concluding anything is wrong on the host.

Verify all three legs, in a fresh login:

```bash
docker info >/dev/null && echo daemon reachable
docker compose version        # the compose *plugin*; prints its own version
                              # (v5.x at this writing), not a literal "v2"
systemctl is-enabled docker   # enabled; the boot unit needs the daemon at boot
```

"Compose v2" names the plugin architecture (`docker compose`, a subcommand),
as opposed to the retired python `docker-compose` binary. The version string
moved past 2.x long ago; what matters is that the subcommand exists.

## 1a. Log rotation — `/etc/docker/daemon.json`

Docker's default `json-file` logging never rotates. Six always-on services
on a 115 GB drive will eventually fill it, and compose.yaml deliberately
carries no per-service `logging:` blocks, so the daemon default is the only
place rotation exists. Write it before starting the stack:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
```

This file was host state from 2026-08-09 that no document recorded; the
2026-08-25 rebuild surfaced it (it only survived the wipe because apt purge
leaves `/etc/docker` alone when it is not empty). A fresh host without it
gets unbounded logs, silently.

Installed, reachable, and Compose v2 present are three different facts and
any one can fail alone.

## 2. The repo clone

The hub runs pushed commits only. `deploy-pi.sh` resets this clone to the
deployed SHA on every run; anything uncommitted in it does not exist as far
as deploys are concerned.

```bash
git clone https://github.com/viperdavethesnake/bellas-reef.git /home/david/bellasreef
```

The path matters: `deploy/compose.yaml` bind-mounts `/home/david/backups`
into the api container, `deploy-pi.sh` expects the clone at
`/home/david/bellasreef`, and `docs/backup-restore.md` writes archives by
those paths. Create the backups directory too:

```bash
mkdir -p /home/david/backups
```

## 3. `deploy/.env` — the deploy secrets

The one file in the clone that is host state rather than repo state
(`git clean` during deploys deliberately spares it). It carries the Postgres
credentials, `BELLASREEF_TAG`, retention, and the device-node group ids that
compose interpolates. Author it per `docs/host-setup.md` §1b before the
first deploy; a missing or half-written `.env` fails compose interpolation
loudly.

## 4. ghcr.io pull credential — DEV ONLY

**A customer never does this step.** Released images are public and
`docker compose pull` needs no login. The login exists because this repo's
packages on ghcr.io are still private while the project is pre-publication,
so a development hub has to authenticate to pull them.

If (and only if) you are deploying from the private packages:

```bash
docker login ghcr.io -u <github-username>
# password prompt: a PAT with read:packages scope, not a password
```

Details, verification, and the credential's location (`~/.docker/
config.json`) are in `docs/host-setup.md` §1c. `deploy-pi.sh` warns before
pulling when the credential is absent.

## 5. Ready check

All of it, from the workstation:

```bash
ssh <pi-host> '
  docker info >/dev/null 2>&1 && echo "docker: reachable" || echo "docker: FAIL"
  docker compose version 2>/dev/null | head -1
  systemctl is-enabled docker
  test -d /home/david/bellasreef/.git && echo "clone: present" || echo "clone: FAIL"
  test -f /home/david/bellasreef/deploy/.env && echo "deploy/.env: present" || echo "deploy/.env: FAIL"
  test -d /home/david/backups && echo "backups dir: present" || echo "backups dir: FAIL"
  grep -q "\"ghcr.io\"" ~/.docker/config.json 2>/dev/null && echo "ghcr login: present (dev)" || echo "ghcr login: absent (fine for public images)"
'
```

Every line green (the ghcr line may honestly read absent), then
`scripts/deploy-pi.sh` from the workstation. On a fresh database that means:
migrations build the schema, the registry is empty, and the telemetry gate
cannot pass until devices are imported. The order after a first deploy is
the TOFU pairing dance, then `bellasreef devices import` (CLAUDE.md,
"Deployment discipline"), then re-run the deploy or watch the wire until a
fresh sample shows.

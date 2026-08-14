# Containers-Only Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The PRD's topology, for real: all five services run as containers on the Pi, images built and pushed by CI to ghcr, deployed by digest-verified tag with the same telemetry gate. The systemd app units are deleted. Approved by David 2026-08-13 ("containers only — we are not carrying that decision").

**Architecture:** CI's existing dry-run multi-arch build starts pushing `ghcr.io/viperdavethesnake/bellasreef-{hardware-io,control-engine,api}` tagged with the git SHA. `deploy/compose.yaml`'s app services gain `image:` refs parameterized on `BELLASREEF_TAG` (keeping `build:` for local dev) plus the hardware surfaces added since the Dockerfiles were written (pinctrl via `/dev/gpiomem0`, device-tree read, PWM sysfs write). One systemd unit (`bellasreef.service`, evolving the spine unit) brings the whole stack up at boot after time-sync; Docker restart policies supervise. `deploy-pi.sh` keeps its refusals and its telemetry-on-the-wire gate but deploys by `compose pull && up`. The hub CLI (`bellasreef pair/revoke/backup/restore/devices`) runs via `docker compose exec api`. Host venv usage by services ends.

**Tech Stack:** Docker/buildx + GHA (`docker/build-push-action`), ghcr, compose. Python images unchanged in shape; hardware-io gains a pinctrl build stage; api gains postgresql-client-17 (PGDG).

## Global Constraints

- Repo `/Users/david/visualstudio/bellasreef`, branch `feat/containers-only` off current main (4361fbc). Backend flow: local gate `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` → push with `BELLASREEF_ALLOW_ENV_SKIPS=1 git push` → PR → CI green → controller merges. THE CUTOVER DEPLOY IS CONTROLLER-DRIVEN (Task 6) — no agent touches the Pi.
- The deploy values that survive verbatim: refuse dirty/unpushed tree; Pi runs pushed commits only; migrations before app start; **fresh telemetry on the wire before reporting success**; spine data services are never restarted by a deploy (postgres/nats/victoria-metrics keep `restart: unless-stopped` and deploys must not force-recreate them — `docker compose up -d` only recreates changed services, and their definitions don't change per-deploy).
- Least privilege is non-negotiable: no `privileged:`, no broad `/sys` rw mount, no root user in app containers. Every new device/mount is specific and commented with why.
- Registry: `ghcr.io/viperdavethesnake/<image>`; CI authenticates with the workflow `GITHUB_TOKEN` (`permissions: packages: write`). The Pi's pull credential is host state, out of scope for CI (controller handles login at cutover).
- Verified host facts the mounts must encode: PWM chip device dir `/sys/devices/platform/axi/1000120000.pcie/1f00098000.pwm` (rw needed for export/duty writes); `of_node` symlinks resolve into `/sys/firmware/devicetree/base` (ro); `pinctrl` needs `/dev/gpiomem0` on Pi 5 (verify at cutover, not assumed — if pinctrl in-container fails with gpiomem0 alone, the cutover step probes which `/dev/gpiomem*` node it wants; they exist as gpiomem0-4); 1-wire mounts already in compose are correct.
- Conventional commits + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: CI publishes images

**Files:** Modify `.github/workflows/ci.yaml`.

- [ ] Read the existing multi-arch dry-run job. Convert/extend it: on push to `main` (not PRs), log in to ghcr (`docker/login-action@v3` with `${{ github.actor }}` / `${{ secrets.GITHUB_TOKEN }}`), build each of the three images with `docker/build-push-action@v6` (`context: .`, the service's `deploy/Dockerfile.*`, `platforms: linux/amd64,linux/arm64`, `push: true`, `tags: ghcr.io/viperdavethesnake/bellasreef-<svc>:${{ github.sha }}` and `:latest`, `cache-from/to: gha`). PRs keep the existing dry-run (build, no push). Add `permissions: { contents: read, packages: write }` at the job that pushes.
- [ ] Keep the job green for forks/PRs (no secrets needed on the dry-run path).
- [ ] Gate locally (`./scripts/check.sh` doesn't lint workflows — `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yaml'))"` is the syntax check), commit: `ci: publish multi-arch images to ghcr on main`.

### Task 2: The images carry their new needs

**Files:** Modify `deploy/Dockerfile.hardware-io`, `deploy/Dockerfile.api`.

- [ ] **hardware-io + pinctrl:** add a builder stage compiling pinctrl from the Raspberry Pi utils repo, pinned by commit:

```dockerfile
FROM debian:bookworm-slim AS pinctrl-build
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake gcc make libc6-dev && rm -rf /var/lib/apt/lists/*
# Pinned: resolve the current default-branch commit of
# https://github.com/raspberrypi/utils at implementation time with
#   git ls-remote https://github.com/raspberrypi/utils HEAD
# and hardcode it here — a moving HEAD is not a build input.
RUN git clone https://github.com/raspberrypi/utils /src \
    && cd /src && git checkout <PINNED_COMMIT> \
    && cd pinctrl && cmake . && make
```

then in the final stage `COPY --from=pinctrl-build /src/pinctrl/pinctrl /usr/bin/pinctrl`. (The service's `PINCTRL` constant is `/usr/bin/pinctrl` — matches.) Replace `<PINNED_COMMIT>` with the real hash; record it in the commit message.
- [ ] **api + pg client tools:** backup/restore shells `pg_dump`/`pg_restore` ≥17; bookworm ships 15. Add the PGDG repo stage-lessly:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
```

- [ ] Verify both images build locally for the host arch: `docker build -f deploy/Dockerfile.hardware-io .` etc. — on this Mac if Docker is present, otherwise note that CI is the build check and rely on Task 1's PR run. Commit: `build(images): pinctrl into hardware-io, pg client tools into api`.

### Task 3: compose.yaml tells the whole truth

**Files:** Modify `deploy/compose.yaml`, `deploy/.env.example`.

- [ ] App services gain `image: ghcr.io/viperdavethesnake/bellasreef-<svc>:${BELLASREEF_TAG:-latest}` above their `build:` (compose uses `image` when present unless `--build`; deploys pull, dev can still `compose build`).
- [ ] hardware-io gains the post-Dockerfile hardware surfaces, each commented:

```yaml
    devices:
      - /dev/i2c-1:/dev/i2c-1
      - /dev/gpiochip0:/dev/gpiochip0
      # pinctrl reads the live pin mux through the RP1's gpiomem window;
      # discovery announces only what the mux proves (capabilities.py).
      - /dev/gpiomem0:/dev/gpiomem0
    volumes:
      - /sys/bus/w1:/sys/bus/w1:ro
      - /sys/devices/w1_bus_master1:/sys/devices/w1_bus_master1:ro
      # PWM chip device dir, rw: export/period/duty writes land here. The
      # /sys/class/pwm entries are symlinks into it; the driver resolves them.
      - /sys/devices/platform/axi/1000120000.pcie/1f00098000.pwm:/sys/devices/platform/axi/1000120000.pcie/1f00098000.pwm
      # Chip identity + of_node compatible resolve into the device tree, ro.
      - /sys/class/pwm:/sys/class/pwm:ro
      - /sys/firmware/devicetree:/sys/firmware/devicetree:ro
    environment:
      BELLASREEF_NATS_URL: nats://nats:4222
```

(Note hardware-io's env in compose currently lacks `BELLASREEF_NATS_URL` — the exact variable whose absence is the documented silent-death trap; add it.) Also add `depends_on: { nats: { condition: service_healthy } }` to hardware-io.
- [ ] api service: mount a backups path (`- /home/david/backups:/backups`) so `bellasreef backup --out /backups/...` lands on the host; add `BELLASREEF_LOG_LEVEL` passthroughs where the env files had them.
- [ ] `.env.example` gains `BELLASREEF_TAG=latest` with a comment (deploys export the SHA).
- [ ] Delete the stale header comment ("declared but commented out") and the dead commented-out service block at the bottom; fix the postgres ports comment (host units are gone).
- [ ] `docker compose -f deploy/compose.yaml config` must succeed with the example env (CI's compose-config job is the gate). Commit: `feat(deploy): compose runs the whole stack — images, hardware surfaces, no host units`.

### Task 4: One boot unit; the app units die

**Files:** Rewrite `deploy/systemd/bellasreef-spine.service` → `deploy/systemd/bellasreef.service`; DELETE `deploy/systemd/bellasreef-{hardware-io,control-engine,api}.service`.

- [ ] `bellasreef.service`: oneshot + RemainAfterExit, `After=time-sync.target docker.service`, `Wants=time-sync.target`, `Requires=docker.service`, ExecStart `docker compose -f /home/david/bellasreef/deploy/compose.yaml --env-file /home/david/bellasreef/deploy/.env up -d --wait`, ExecStop `... stop` (stop, not down — volumes and networks persist). WorkingDirectory the clone. Comment block: this unit exists for boot persistence and clock ordering; Docker's restart policies are the supervisor; deploys talk to compose directly and only `start` this unit.
- [ ] Delete the three app unit files. Commit: `feat(deploy): one boot unit; the systemd app units are deleted, per PRD topology`.

### Task 5: deploy-pi.sh deploys containers

**Files:** Rewrite `scripts/deploy-pi.sh` (keep its refusal/verification skeleton — read it fully first).

- [ ] Keep: dirty/unpushed refusal, CI-green check for the SHA, contracts-version read, git reset of the Pi clone, avahi record install/reload, the `/info` contract check, and the **fresh-sample telemetry gate** verbatim in spirit.
- [ ] Replace the middle: no `uv sync`, no unit installs beyond `bellasreef.service`, no `systemctl restart` of app units. New sequence on the Pi: `docker login ghcr.io` preflight (fail with instructions if not logged in — the credential is host state); `BELLASREEF_TAG=<sha> docker compose pull hardware-io control-engine api`; migrations via one-off: `BELLASREEF_TAG=<sha> docker compose run --rm api alembic upgrade head` (confirm the alembic invocation the old script used and mirror it inside the container — read the old script's migration block); write the tag where boot can see it (append/update `BELLASREEF_TAG=<sha>` in `deploy/.env` on the Pi — sed-replace, never duplicate); `docker compose up -d --wait hardware-io control-engine api` (data services untouched unless their pinned digests changed); `systemctl start bellasreef.service` remains for boot enablement.
- [ ] Digest verification: after pull, `docker compose images` the three services and print image digests into the deploy log — the pinned-by-digest audit trail.
- [ ] Commit: `feat(deploy): deploy-pi.sh pulls digest-verified images; the telemetry gate survives`.

### Task 6 (controller): cutover on the Pi

- [ ] Merge the PR after CI green (CI now also publishes images for the merge SHA — wait for that run).
- [ ] Pi: `docker login ghcr.io` with the pull credential (try `gh auth token` from the Mac first — if its scopes include `read:packages` it is David-authorized; otherwise ask David for a fine-grained PAT).
- [ ] Run the new `deploy-pi.sh`. Then the drills, containerized: `docker kill` hardware-io → restart policy revives it, telemetry resumes; `docker compose stop nats` 30 s → engine/api reconnect (existing reconnect logic), telemetry resumes; full reboot → stack up ordered after time-sync, all services healthy, telemetry flows, **pinctrl works in-container** (journalctl/docker logs hardware-io shows a 4-channel announcement, registry still 4 rows). Any failure: stop, diagnose, fix-forward on the branch — do not improvise on the Pi.
- [ ] Disable/remove the old app units on the Pi (`systemctl disable --now` + rm from /etc/systemd/system) — after the containerized stack passes the drills, not before.
- [ ] App smoke: sim still paired, Tank streaming, Hardware section 4 channels.

### Task 7: Docs tell the container truth

**Files:** CLAUDE.md (Deployment discipline + relevant Verified-host-facts lines + the three standing FLAGs), docs/host-setup.md (§1b, §7 rewritten, §10 CLI via `docker compose exec api`, §11 pg-tools note now points at the api image), docs/backup-restore.md (hub flows return to `docker compose exec api bellasreef ...`, `--out /backups/...`).

- [ ] CLAUDE.md deployment section: rewritten around compose (values kept: supervised, pushed commits only, telemetry gate, spine-data-not-restarted); the three topology FLAGs close with the resolution recorded in one line each (containers won; LAN-exposure closed by David's no-rotation ruling 2026-08-13; ordering solved by compose depends_on + one boot unit). Note the host-unit era as a dated, closed detour.
- [ ] host-setup: `uv`/venv references for services go; the CLI section teaches `docker compose exec api bellasreef pair` etc.; pull-credential setup documented as host state (§1b sibling).
- [ ] backup-restore: on-hub flows via compose exec (the api image carries pg 17 tools; `--out /backups` is the host mount); fresh-hardware restore updated to the container reality (steps get shorter).
- [ ] Commit: `docs: the deployment story is containers, and the FLAGs close`.

### Task 8 (controller): closeout

- [ ] Memory updated (topology resolved, cutover state, credential note); DeviceView-channel unit and the output-stage FLAG are next; then the v0.1.0 tags.

## Self-Review

- PRD conformance is the point; least-privilege preserved (specific nodes/mounts, still no privileged, non-root user); the CLI story (pair/revoke/backup on the hub) has a container answer including pg tools and a backups mount; the silent-death env var is explicitly added; migrations containerized; boot ordering via one unit keeps After=time-sync.
- Known risks, named: pinctrl-in-container device node (probed at cutover, gpiomem0-4 exist); PWM sysfs rw bind is device-dir-specific (matches identity-resolution); ghcr pull credential is a David-provided host credential unless `gh auth token` suffices; buildx arm64 emulation makes CI slower (~minutes, acceptable).

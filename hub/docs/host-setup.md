# Host setup

The **only** mutations made to the Pi outside containers. Everything else the
platform needs runs in Docker with specific device nodes passed to `hardware-io`
and nothing privileged.

Keep this file exhaustive. If a host change is needed and it is not written
here, that is a bug in this document, not a licence to make the change quietly.

Values below are the measured state of the reference host as of 2026-08-09; see
"Verified host facts" in CLAUDE.md for the full inventory.

This file is the procedure for the **Raspberry Pi 5 specifically**, and the
reference for what each host change is for. For a first install, run
`scripts/install-hub.sh` on the hub instead: it checks the requirements, takes
an inventory of the hardware, writes `deploy/.env`, deploys the stack and
verifies it came up — offering to install what is missing, and reporting the
boot-config changes it will not make for you. While the images are private, a
`docker login ghcr.io` (§1c below) is still a manual prerequisite: the script
names the command when the pull is refused, but it does not manage
credentials. For what any machine has to provide before either is worth
running, see `docs/hub-platform-requirements.md`.

For the runtime prerequisites themselves (Docker Engine with Compose v2, the
repo clone, `deploy/.env`, and the ghcr.io login while images are private),
see [`../README.md`](../README.md); this file assumes them and does not
install them.

---

## 1. Device tree overlays

In `/boot/firmware/config.txt`:

```ini
dtparam=i2c_arm=on

[pi5]
dtoverlay=w1-gpio-pi5,gpiopin=4
```

**The `-pi5` suffix is required.** Plain `dtoverlay=w1-gpio` is the pre-Pi-5
variant; it loads without error and silently fails to bring up the bus. This
costs a reboot cycle to discover.

Never put a trailing `#` comment on a `dtparam=` or `dtoverlay=` line — the
firmware device-tree parser can fold the comment into the value.

After changing this file, reboot. Verify:

```bash
ls /sys/bus/w1/devices/     # expect w1_bus_master1
ls /dev/i2c-1
gpiodetect | grep rp1       # expect gpiochip0 [pinctrl-rp1] (54 lines)
```

## 1b. `deploy/.env` — the second host-state file

Alongside the dtoverlays above, `~/bellasreef/deploy/.env` is host
state, not repo state: it is gitignored (`deploy/.env.example` is the
template committed instead) and holds the values `bellasreef.service`
interpolates into `deploy/compose.yaml` — `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`, `BELLASREEF_DATABASE_URL`, `NATS_URL`,
`BELLASREEF_TAG`, `VM_RETENTION`, `I2C_GID`, `GPIO_GID`.

`I2C_GID`/`GPIO_GID` are required in this file for the same reason every
other value here is: `bellasreef.service` brings up the whole stack,
hardware-io included, in one `docker compose up`, and compose interpolates
the whole file up front — a missing variable anywhere in it fails that `up`
before any service, hardware-accessing or not, gets a chance to start.

Never committed, and never reset by the hub scripts: `install-hub.sh` leaves
an existing `deploy/.env` untouched, and `update-hub.sh`'s `git checkout` to a
new tag does not touch a gitignored file. Verify it exists and is current after any change
to Postgres credentials, retention, or the host's `i2c`/`gpio` group GIDs
(`getent group i2c gpio`).

## 1c. The ghcr.io pull credential — the third piece of host state

Containers-only means the app images (`hardware-io`, `control-engine`, `api`)
are pulled, not built, on the Pi — and ghcr.io packages under this repo are
private, so pulling them needs a credential the Pi has to hold on its own.
Without it, `docker compose pull` fails, and `install-hub.sh` (or
`update-hub.sh`) catches the auth failure and stops before it touches
anything running.

```bash
docker login ghcr.io -u <github-username>
# password prompt: paste a token with the read:packages scope — a
# fine-grained PAT scoped to this repo's packages, or a classic PAT with
# read:packages. Not a password; GitHub does not accept those here.
```

The credential lands in `~/.docker/config.json` (mode `0600` by `docker
login` itself) and is host state, not repo state — never committed, never
reset by the hub scripts, and not recreated by a factory wipe. Verify it
survived a wipe or a fresh clone with:

```bash
grep -q '"ghcr.io"' ~/.docker/config.json && echo present
```

`install-hub.sh` and `update-hub.sh` catch a pull refused for this reason and
print the login command above, rather than letting a bare `docker compose
pull` fail with an opaque auth error partway through a deploy.

## 2. Headless stripping

The reference host runs with no display stack: `vc4-kms-v3d` removed, audio off,
camera and DSI auto-detect off. This drops all HDMI audio modules with it
(`snd` module count 0).

Consequence worth knowing before you need it: **the serial console is the only
live out-of-band console.** `serial-getty@ttyAMA10` runs and
`console=serial0,115200` is in `cmdline.txt`, so a USB-TTL adapter on the 3-pin
debug connector gives a real login. Offline recovery is to power down, pull the
boot drive, and mount the vfat boot partition on another machine to fix
`config.txt`.

Timestamped backups of every edit sit beside the file as `config.txt.bak-*`.

## 3. Clock — mandatory, this host has no RTC battery

A power cut loses the time, and a scheduler acting on a wrong clock doses at the
wrong hour. Three packages, all enabled:

```bash
sudo apt-get install -y chrony fake-hwclock
sudo systemctl enable chrony chrony-wait fake-hwclock-load fake-hwclock-save
```

| Unit | Role |
|---|---|
| `fake-hwclock-load` / `-save` | restores the saved time at boot, before any network |
| `chrony` | corrects once the network is up; `makestep 1 3` steps rather than slews |
| `chrony-wait` | makes `time-sync.target` mean *actually synchronised* |

`chrony` replaces `systemd-timesyncd` (apt removes it automatically). It is
chosen over timesyncd because timesyncd is a plain SNTP client that slews — on a
box that boots with a wrong clock you need a step, not a crawl.

`fake-hwclock.service` and `hwclock.service` show as **masked**. That is the
package masking its own legacy sysvinit units, not a fault. Do not "fix" it.

**Every service that acts on time must be ordered against this:**

```ini
[Unit]
After=time-sync.target
Wants=time-sync.target
```

An unsynchronised clock is a fault state that holds actuators at safe state — it
is not a reason to guess.

## 4. Container hardware access

`hardware-io` is the only container with device access. Specific nodes, never
`privileged: true`:

```yaml
devices:
  - /dev/i2c-1:/dev/i2c-1
  - /dev/gpiochip0:/dev/gpiochip0
volumes:
  - /sys/bus/w1:/sys/bus/w1:ro        # DS18B20 read path
  - /sys/devices/w1_bus_master1:/sys/devices/w1_bus_master1:ro
```

The 1-Wire mount is read-only and sysfs-based because `w1-therm` exposes no
character device. `/dev/gpiochip0` is passed by path, but the driver still
resolves the chip **by label** inside the container — the index is not
guaranteed stable across kernels.

The container user needs the host's `i2c` and `gpio` group GIDs. Nothing needs
root.

## 5. Networking

The host is reached by mDNS name, never by IP — the WiFi lease changes.

`/etc/avahi/avahi-daemon.conf` must pin the real interfaces:

```ini
allow-interfaces=eth0,wlan0
```

Without this, avahi also advertises Docker's `docker0` bridge (`172.17.0.1`),
which is unreachable from the LAN — clients then intermittently resolve the host
to an address that does not work. An allowlist is used rather than denying
`docker0` because Docker also creates `br-*` bridges for user-defined networks
and avahi has no wildcard syntax.

### Service discovery: `_bellasreef._tcp`

A hostname A record is not enough. An app that only knows `bellasreef.local`
cannot tell a reef controller from anything else answering to that name, and
cannot learn the API port. `auth.md` step 1 browses for the service type, so
it has to be registered:

```bash
sudo cp deploy/avahi/bellasreef.service /etc/avahi/services/bellasreef.service
sudo systemctl reload avahi-daemon
```

Verify from another machine, not from the Pi — avahi does not reliably
reflect its own services back to a local browse, so a local check that finds
nothing proves nothing:

```bash
dns-sd -B _bellasreef._tcp            # macOS
avahi-browse -rt _bellasreef._tcp     # Linux
```

Verified 2026-08-09: advertises as `Bella's Reef on bellasreef`, discovered
from the dev Mac. The TXT records carry the API base path and the contracts
version, so a client can refuse a hub it is too old to talk to.

WiFi power save is disabled and persisted in the NetworkManager connection
profile:

```bash
sudo nmcli con modify "<ssid>" 802-11-wireless.powersave 2
```

## 6. Power-pull drill — bench procedure

PRD G2 requires that pulling power mid-cycle leaves every actuator in its safe
state. **No software can assert this**, which is why it is a documented
procedure rather than a test: it is a property of the *wiring*, and the
controller is not running at the moment that matters.

The automated drills (`services/hardware_io/tests/test_drills.py`) cover process
kill, container kill and spine outage. This covers the one they cannot.

### Prerequisite, and the whole point

**Every relay must be wired normally-open**, so the de-energised state is the
safe state. If a relay is wired normally-closed, this drill fails no matter what
the software does — loss of power *energises* the load. Verify this with a meter
before the first drill, not by reading the wiring diagram.

The same applies to any equipment with its own latching behaviour: a heater
controller that remembers "on" across a power cycle defeats the drill
regardless of the relay.

### Procedure

Run on the bench with dummy loads (indicator lamps are ideal — you can see the
result), never on a stocked tank.

1. Bring the stack up and confirm `hardware-io` is healthy.
2. Drive every actuator to its **non-safe** state, deliberately. A drill that
   starts with everything already off proves nothing.
3. Confirm each load is energised — visually, not from the UI. The UI reports
   what was commanded; the lamp reports what happened.
4. **Cut mains power at the wall**, not by shutting down the Pi. A graceful
   shutdown exercises the software path, which is already covered.
5. Observe every load within one second. All must be de-energised.
6. Restore power. Observe through the full boot: no load may energise at any
   point during boot, including before `hardware-io` starts. A relay board that
   glitches on during GPIO initialisation is a real failure and this is the only
   way it is ever seen.
7. Confirm the stack comes back and every actuator reports its safe state.

### Pass criteria

- All loads de-energised within 1 s of power loss.
- No load energises at any point during the subsequent boot.
- After boot, every actuator reports safe state and none is latched from the
  power event.

### Recording

Log each run in the audit trail with date, firmware/image tags, and the
actuators covered. Re-run after any change to relay wiring, the relay board, the
GPIO pin map, or `config.txt`.

A failure here is a wiring defect. Do not attempt to compensate for it in
software — there is no software running at the moment this drill tests.

## 7. The one boot unit, and container supervision

Containers-only, per the PRD topology (David's ruling, 2026-08-13): all five
services — nats, postgres, victoria-metrics, hardware-io, control-engine,
api — run as containers under `deploy/compose.yaml`, and Docker's own
`restart: unless-stopped` policies are the supervisor from there on. There
are no per-service systemd units; `bellasreef-{hardware-io,control-engine,
api}.service` are deleted, not deprecated. There are no dev launchers either;
`scripts/dev/run-*.sh` were deleted on 2026-08-11 after an unsupervised
hardware-io exited and stayed exited for ten hours. See CLAUDE.md,
"Deployment discipline".

**Closed detour, dated:** between 2026-08-11 and 2026-08-13 this section
described three separate systemd app units reading
`/etc/bellasreef/<service>.env`. That was an overcorrection from the outage —
it put process supervision on the host instead of in the runtime CLAUDE.md
already locked. Removed 2026-08-13; nothing below describes it.

One unit remains: `bellasreef.service` — oneshot + `RemainAfterExit`, ordered
`After=time-sync.target docker.service` / `Wants=time-sync.target`. It exists
for exactly two things: bringing the stack up once at boot, and clock
ordering (§3 — a board with no RTC battery must not schedule against an
unsynchronised clock). It is not the supervisor; Docker is. A deploy `start`s
this unit and never `restart`s it — restarting it would `up -d --wait` the
whole stack including the spine's data services, which is the durable-
contention risk the environment-boundary rule in CLAUDE.md exists to prevent.

Deploy with `scripts/install-hub.sh`, run on the hub from the `bellasreef-hub`
clone: it pulls the three app images at the commit-sha tag recorded in
`deploy/release.env`, migrates, brings the stack up, and then phase 6 checks
that every compose service is running, `bellasreef.service` is enabled and
rendered for this host, the API answers on port 8000, avahi's service record
is published, and prints the setup code. `scripts/update-hub.sh` is meant to
move an already-installed hub to a newer release the same way; it is not
implemented yet (see `../README.md`), so an update today is a fresh
`install-hub.sh` run from the new release, or the manual steps below.

None of that reads a telemetry sample. Until `update-hub.sh` lands, confirm
telemetry is actually on the wire by hand by asking VictoriaMetrics directly:

```bash
curl -s 'http://127.0.0.1:8428/api/v1/query?query=count({__name__=~"bellasreef.*"})'
```

### Service configuration: `deploy/.env` + compose, not a directory per service

There is no `/etc/bellasreef/<service>.env` anymore. Configuration is two
things:

- `deploy/.env` (§1b) — host state, gitignored, holds `POSTGRES_*`,
  `BELLASREEF_DATABASE_URL`, `NATS_URL`, `BELLASREEF_TAG`, `VM_RETENTION`,
  `I2C_GID`/`GPIO_GID`.
- the `environment:` block of each service in `deploy/compose.yaml` — repo
  state, committed, machine-agnostic (it references `${VARS}` from
  `deploy/.env`, not literal values).

`BELLASREEF_VM_URL` is not optional decoration: `bellasreef backup` refuses to
run without it (or an explicit `--no-telemetry-snapshot`), and it is set in
the `api` service's `environment:` block in compose.yaml, not per-host.

`BELLASREEF_NATS_URL` is the entry that bites. Leave it unset on hardware-io
and it reads the probe, serves metrics, logs a clean startup, and publishes
nothing at all. Nothing about the container looks wrong; the tank is simply
not monitored. That is why `install-hub.sh`'s verify phase checks for a
sample on the wire instead of a container being merely "up."

### Logs are `docker compose logs`, not journald-per-unit

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env logs -f hardware-io
docker compose -f deploy/compose.yaml --env-file deploy/.env logs --since "2 hours ago" hardware-io
# or, equivalently, straight from the container:
docker logs -f bellasreef-hardware-io-1
```

Not `/tmp/*.log`. A log in `/tmp` is truncated by the next start, which is how
the evidence for the first half of the 2026-08-10 outage was destroyed before
anyone read it — the same reasoning, now pointed at compose's own log driver.

### Installing by hand

`install-hub.sh` does this for you; the manual form is here for a first
bring-up.

```bash
sudo install -m 0644 deploy/systemd/bellasreef.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bellasreef
```

That one `enable --now` brings up the whole stack — spine and app services
together — because compose's own `depends_on: { condition: service_healthy }`
sequences hardware-io/control-engine/api after nats/postgres are ready; there
is no separate "bring the spine up first" step to remember on a machine being
*restored* rather than deployed (see `docs/backup-restore.md`).

The unit is ordered `After=time-sync.target` / `Wants=time-sync.target`. On a
board with no RTC battery that ordering is the difference between a scheduler
starting against a real clock and one starting against whatever `fake-hwclock`
restored. It only means anything because `chrony-wait` is enabled (§3) — without
it, `time-sync.target` is reached as soon as the NTP daemon starts, which is not
the same claim.

RESOLVED (2026-08-14): the FLAG that stood here described a per-service systemd
watchdog (`WatchdogSec` on `bellasreef-hardware-io.service`) from the deleted
host-units era, and a drill script that targeted that unit. Both are gone. The
subsections below are rewritten from a drill actually run on this hardware, and
the `sd_notify` half of the mechanism was deleted from the code rather than
documented as an option that no deployed path can reach.

### One runtime, one liveness mechanism

CLAUDE.md locks Docker Compose as the runtime, and **Docker does not restart a
container that is merely unhealthy** — `restart: unless-stopped` acts on process
*exit*. A healthcheck alone therefore cannot recover a hung event loop in a
container; something has to make the process die.

That something is `LivenessGuard`: a thread watching a heartbeat emitted from
inside the supervisor loop, which calls `os._exit(70)` when the beat goes stale.
The process exits, and `restart: unless-stopped` brings it back.

It runs on a **thread**, not an asyncio task, because a frozen event loop cannot
run the task that would have rescued it. The beat is emitted from *inside* the
loop for the same reason — a beat from a separate task would keep asserting
health the loop no longer has.

`BELLASREEF_LIVENESS_TIMEOUT_S` (15 s in `compose.yaml`) is the only liveness
number in the system. The `sd_notify` path that used to sit beside this one was
deleted on 2026-08-14: under Compose there is no `NOTIFY_SOCKET`, so it was a
branch that could not execute anywhere, and a dormant second answer to "is this
process alive" reads as a supported option.

### `docker kill` does not exercise this path

Measured on this Pi, docker 29.7.2, 2026-08-14 — worth knowing before you try to
test recovery by hand:

| How the process is made to die | Guard fires | Container restarts |
|---|---|---|
| `docker kill --signal=USR1 <c>` | yes, exit 70 | **no** — stays `exited`, `RestartCount=0` |
| `os.kill(1, SIGUSR1)` from inside | yes, exit 70 | **yes** — new PID, `RestartCount` +1, ~15 s |

`docker kill` marks the container manually-stopped, and `unless-stopped` then
declines to restart it. This is an artefact of the daemon's kill API, **not** of
the recovery path: a genuine stall exits the process with nobody calling `docker
kill`, which is the second row. `scripts/drill-restart.sh` signals PID 1 from
inside the container for exactly this reason.

### Telling the death modes apart

The liveness guard exits **70** (`EX_SOFTWARE`) deliberately, so a post-incident
`docker inspect` says which mechanism fired:

| Exit | Meaning |
|---|---|
| `0` | clean stop |
| `70` | **liveness guard — the supervisor loop stalled** |
| `137` | SIGKILL / OOM killer (128+9) |

```bash
# only meaningful while the container is stopped — a running container
# reports ExitCode 0 even if its previous life ended at 70
docker inspect --format '{{.State.ExitCode}}' bellasreef-hardware-io-1

# after a restart-policy recovery, the exit is in the event log instead
docker events --since 30m --until "$(date +%s)" \
  --filter container=bellasreef-hardware-io-1 --filter event=die \
  --format '{{.Actor.Attributes.exitCode}}'
```

A stall and an OOM kill send you to completely different places, and at 3 a.m.
the exit code is the fastest way to know which.

### Metrics are not published to the host

`hardware-io` uses `expose:` rather than `ports:`. Health and metrics ride the
compose network only — victoria-metrics scrapes `http://hardware-io:9101/metrics`
from inside it. The one container holding `/dev` should not also own a listening
socket on the host.

### Running the drill

```bash
./scripts/drill-restart.sh bellasreef.local   # from the dev machine
./scripts/drill-restart.sh                    # on the Pi
```

Arming is handled by the script. The freeze trigger is read once at process
start, so arming means recreating hardware-io with `deploy/compose.drill.yaml`
layered on — and the script **disarms via a `trap` on every exit path,
including failure**. Nothing to remember, and nothing left behind: an
always-armed "hang yourself" signal on a tank controller is a liability, and a
drill that leaves one has broken the thing it was checking.

The drill asserts four things, and the third is the one that matters:

1. the container **died with exit 70** on its `die` event — the liveness guard
   fired, rather than the restart having some incidental cause.
2. the guard **logged the stall** before terminating.
3. the restarted process **re-ran the startup safe-state assertion** — the
   `drill-dummy` actuator deliberately comes up *energised*, so a restart that
   skipped the assertion would be visible rather than silently passing.
4. the container healthcheck is green again.

Verified 2026-08-14 on this hardware: freeze engaged, guard fired at
`stall_s 15.496` against a 15 s timeout, process exited 70, container restarted
(`RestartCount` 0→1, new PID), safe state re-asserted with
`drill_actuator_safe:true` about a second later — roughly 16 s from hung loop to
hardware safe.

If the drill fails it dumps the container's logs, state and die events **before**
the trap disarms, because recreating the container destroys its logs. The first
failing run on 2026-08-14 lost its own evidence that way.

## 8. Not configured yet

- **Firewall.** Nothing listens yet. Must be revisited before the API is
  exposed. Note Docker writes its own nftables rules and will conflict with a
  naive host firewall config.
- **Unattended upgrades.** Deliberately absent so nothing reboots mid-dose. The
  trade is that patching must become a scheduled, supervised action.

## 8b. Wiring reference

**There is no current wiring sheet.** `artifacts/bellasreef-day1-wiring.pdf` was
removed on 2026-08-12: it showed the withdrawn design with a 10 V pull-up
directly on a PCA9685 LEDn pin, which is out of spec at a 5.5 V absolute
maximum. A drawing that is wrong is worse than no drawing, because somebody
builds from it.

Until a corrected sheet exists, the wiring reference is:

- **CLAUDE.md "Verified host facts"** — measured device inventory, bus paths,
  and the correction trail for the withdrawn design.
- **The output-stage table** in that section (item 0a): the external N-FET
  stage, gate resistor, drain to the dim line, source to ground, and the DMM
  probe point.

Both are text, both are dated, and both distinguish what has been measured from
what has only been ruled.

## 9. The device file, and the PWM overlay

### Devices are declared in the registry, not in a file

hardware-io builds its devices from **registry assignments**, read off the NATS
spine at startup. There is no device file in its startup path and it reads
nothing from disk.

That is a deliberate reversal. `/etc/bellasreef/devices.yaml` used to be the
source, and it let a config author choose an id for hardware that already had
one in the registry — a tank's history forked across two `device_id`s for
seventy minutes before it was caught. The ROM is the hardware's identity, the
`device_id` is the registry's, and a file that can mint a third is a file that
will.

Devices are created by binding an announced capability:

```bash
# What this hub's hardware can offer, and what is already claimed.
GET /api/v1/capabilities

# Claim one.
POST /api/v1/devices
```

`/etc/bellasreef/devices.import.yaml` on this host is an **input to
`bellasreef devices import`** and nothing else. Importing it goes through the
same endpoint with the same validation, including adopt-rather-than-duplicate.
It is kept for seeding a rebuilt hub; deleting it changes nothing at runtime.

Find probe ROM codes with:

```bash
ls /sys/bus/w1/devices/ | grep '^28-'
```

### PWM overlay — host mutation, and it needs a reboot

The RP1 PWM block is present without any overlay: `pwmchip0` at
`1f0009c000.pwm`, `npwm` 4 (measured 2026-08-11, kernel 6.18.39). What the
overlay does is **mux header pins to it**. Without one, exporting a channel
succeeds and drives nothing.

```bash
# In /boot/firmware/config.txt, [pi5] section. Applied and verified 2026-08-13.
dtoverlay=pwm-4chan
```

`pwm-4chan` is **our own overlay** — source in `deploy/overlays/pwm-4chan.dts`,
build/install commands in its header — because no stock overlay muxes all four
RP1 PWM0 channels. The verified pin map:

| Channel | GPIO | Header pin | legacy `func` | RP1 alt |
|---|---|---|---|---|
| PWM0_CHAN0 | 12 | 32 | 4 | a0 |
| PWM0_CHAN1 | 13 | 33 | 4 | a0 |
| PWM0_CHAN2 | 18 | 12 | **2** | a3 |
| PWM0_CHAN3 | 19 | 35 | **2** | a3 |

The legacy `brcm,function` values are translated per-pin and are not the RP1
alt numbers — `func=7` on 18/19 is rejected and **poisons the whole map**,
unmuxing 12/13 too (measured; the two `pinctrl-rp1 ... invalid function` lines
in dmesg are the tell). hardware-io's discovery reads the live mux with
`pinctrl get` at startup, so whatever this overlay muxes is exactly what the
hub announces — fewer channels muxed means fewer announced, no code change in
either direction. The earlier two-channel form
(`dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4`) remains valid if only
two channels are wanted.

**Not two single-channel overlays.** The archived HAL (v3.1.0) prescribes

```
dtoverlay=pwm,pin=12,func=4
dtoverlay=pwm,pin=13,func=4
```

and that was tried first, on this board, and **does not work**: the second line
wins and the first is silently discarded. Measured after the reboot —

```
12: no    pd | -- // GPIO12 = none
13: a0    pd | lo // GPIO13 = PWM0_CHAN1
```

`pwm-2chan` produces both:

```
12: a0    pd | lo // GPIO12 = PWM0_CHAN0
13: a0    pd | lo // GPIO13 = PWM0_CHAN1
```

The archive's channel mapping is otherwise correct — **channel 0 → GPIO12,
channel 1 → GPIO13** — it is only the overlay form that does not carry to RP1.

**Two pwmchips exist after the overlay loads**, and the index moved:

| node | device | which |
|---|---|---|
| `pwmchip0` | `1f00098000.pwm` | the one the overlay muxes to GPIO12/13 |
| `pwmchip1` | `1f0009c000.pwm` | present before the overlay; unmuxed |

Before the overlay, `pwmchip0` **was** `1f0009c000.pwm`. The index is not
stable across a config change, exactly like `gpiochip*` — which is why
hardware-io resolves the chip by device identity (`1f00098000.pwm` = PWM0,
ours; `1f0009c000.pwm` = PWM1, the fan header's block) and never by index.

Verify with `pinctrl get 12,13,18,19` after a reboot; a channel that exports
happily while the pin reads `none` is the failure this section exists to
prevent.

Three cautions:

- **Never put a trailing `#` comment on a `dtoverlay=` line.** The firmware DT
  parser can fold it into the value. Same rule as the 1-Wire overlay in §1.
- **`npwm` disagrees with the archive.** Ours reads **4**; the archive states it
  should read 2 and calls two channels the hardware reality. Recorded in
  CLAUDE.md, unresolved. hardware-io announces whatever `npwm` reports rather
  than assuming a count, so this disagreement does not have to be settled before
  the registry works — but it should be settled before anyone trusts channels
  2 and 3.
- The channel→GPIO mapping is reported to clients as a convenience and **has not
  been confirmed on this board**. It is a wiring fact; confirm it before binding
  anything to a pin.

### PWM sysfs permissions — no rule needed, and why it looked like one

**Correction, 2026-08-12.** An earlier revision of this file said a udev rule
was required. It is not: Raspberry Pi OS already ships one in
`/usr/lib/udev/rules.d/99-com.rules`, and it is correct.

```
SUBSYSTEM=="pwm", ACTION!="remove", PROGRAM="/bin/sh -c 'chgrp -R gpio /sys%p && chmod -R g=u /sys%p'"
```

What actually happened is a race, measured on this host:

```
immediately after export:  -rw-r--r-- root root   -> EACCES
~300 ms later:             -rw-rw-r-- root gpio   -> writable
```

Export creates the attributes owned `root:root`; udev chgrps them to the `gpio`
group a moment afterwards, and the two are not atomic. hardware-io waited for
the channel *directory* and then wrote immediately, landing in the window where
the files exist and it has no permission to them. The service was simply faster
than udev.

The driver now waits for `duty_cycle` to be **writable**, not merely present,
and the fake in its tests models the delay so the race cannot come back.

Worth keeping as a caution: "we need a udev rule" was a plausible reading of
`PermissionError`, and it was wrong. Check whether the permission arrives late
before concluding it never arrives.

After editing config.txt, reboot and confirm the channel exports:

```bash
echo 0 | sudo tee /sys/class/pwm/pwmchip0/export
ls /sys/class/pwm/pwmchip0/pwm0/     # period duty_cycle polarity enable
echo 0 | sudo tee /sys/class/pwm/pwmchip0/unexport
```

`/sys/class/pwm` is **not** the forbidden sysfs GPIO interface. `/sys/class/gpio`
is deprecated and absent on this board; `/sys/class/pwm` is the current kernel
PWM ABI and the only interface the RP1 PWM block exposes. libgpiod does not
cover PWM — it is a GPIO character-device library, and hardware PWM is not GPIO.

## 10. Getting back in: `bellasreef pair` and `bellasreef revoke`

Two commands on the hub, for the two halves of losing a phone. They are the
only terminal interaction in the whole system, and you should need them roughly
never. Read this before you need it, because the day you need it is the day the
app will not open.

A related use of pairing: `bellasreef devices import` needs an access token
(`--token`, or `BELLASREEF_TOKEN` in the environment) from a paired client, and
this repo's CLAUDE.md, "Deployment discipline," documents the seed-a-client-
then-revoke-it sequence for getting one.

### Running the CLI

`bellasreef` is a console script installed with the API package. It lives
**in the `api` image**, not in a host virtualenv — there is no
`.venv/bin/bellasreef` on the host to fall back to anymore. It talks to
Postgres directly and does not go through the API, which is the point: the
API is what you cannot authenticate to.

Run it via `docker compose exec` against the already-running `api`
container, which means it inherits that container's own environment —
no sourcing a service env file by hand, because there is no longer a
per-service env file to source (see §7):

```bash
cd ~/bellasreef
docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef revoke --list
```

| Variable | What it is for | Missing |
|---|---|---|
| `BELLASREEF_DATABASE_URL` | everything. Required. | refuses, exit 2 |
| `BELLASREEF_NATS_URL` | publishing the audit event | the command still does its job, then warns on stderr that the event was **not** recorded |

Both are set in the `api` service's `environment:` block in
`deploy/compose.yaml` — there is nothing to source by hand, and nothing to
get wrong by sourcing only half of it.

### Listing clients

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef revoke --list
```

```
2 client(s) ever paired, 1 still live.

  0f9e4c6a-1d2b-4a77-9f3e-1c5b8a2d4e60  David's iPhone
      paired 2026-08-09T18:06:12+00:00 · last seen 2026-08-12T14:20:01+00:00 · live
  6b1c0d55-9a4e-4f21-8d77-2e0a3c9b7f14  iPhone
      paired 2026-07-30T11:02:44+00:00 · last seen 2026-08-01T07:55:10+00:00 · REVOKED 2026-08-01T08:10:00+00:00
```

`--json` gives the same rows machine-readably. Nothing is revoked by `--list`.

### I lost my phone (and I still have another paired device)

Revoke the lost one by id, or by name when the name is unambiguous:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef revoke 0f9e4c6a-1d2b-4a77-9f3e-1c5b8a2d4e60
docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef revoke "David's iPhone"
```

If two clients share a name, the command lists them and revokes nothing. Every
iOS device pairs as "iPhone" today (`UIDevice.current.name` returns the model),
so expect to use ids.

A revoke takes effect on the phone's next request. Its refresh token is dead and
its access token stops working, because liveness is checked per request rather
than waiting for the JWT to expire. One exception: a WebSocket that is already
open keeps streaming until the socket drops.

### I am locked out entirely

The lost phone was the only paired device, so there is nobody left to approve a
new one and the open-pairing window has been shut since the first device paired.
Three steps, on the hub:

```bash
cd ~/bellasreef

# 1. Open a recovery window. Default 300 seconds; --ttl takes seconds.
docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef pair --ttl 600

# 2. On the replacement phone, open the app, pick the hub, pair.
#    It gets a token immediately instead of a six-digit code, because the
#    window is open. The window is spent by whoever uses it first.

# 3. Turn the lost phone off.
docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef revoke --list
docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef revoke <id of the lost phone>
```

**Step 3 is not optional, and step 1 does not do it for you.** A pairing window
*adds* a client. It does not clear client state, deliberately: the TOFU window
that let your first device in is keyed on client rows having ever existed, so
deleting revoked clients to "reset" the hub would reopen open pairing to anyone
on the LAN. The recovery would be undoing the protection it is recovering from.
Pair the replacement, then revoke the old one. Two commands, on purpose.

If the window expires before you get to the app, run `pair` again. There is no
cancel; a window is spent or it ages out.

### Confirming the audit row landed

Both commands publish to `bellasreef.audit.auth`, and the writer persists it to
`audit_log`. From the new phone, or with a token:

```bash
curl -sH "Authorization: Bearer $TOKEN" \
  'http://bellasreef.local:8000/api/v1/audit?category=auth&limit=5' | jq .
```

Look for `pair.window_opened` and `client.revoked`. If the CLI warned that
`BELLASREEF_NATS_URL` was unset, they will not be there, and there is no way to
add them after the fact: `audit_log` is append-only by trigger.

## 11. PostgreSQL client tools — now carried by the `api` image

`bellasreef backup` and `bellasreef restore` spawn `pg_dump`/`pg_restore`, and
containers-only changed where those binaries live. The command now runs
*inside* the `api` container (`docker compose exec api bellasreef backup
--out /backups/...`), and `/backups` is itself a host bind mount
(`${BELLASREEF_BACKUP_DIR}:/backups`, declared on the `api` service in
`deploy/compose.yaml`; the installer sets `BELLASREEF_BACKUP_DIR` to
`~/backups`), so `pg_dump --file` writing inside the container's filesystem lands the
archive on the host through that mount — no `docker exec` detour needed, and
none of the "host copy of the tools" reasoning below is load-bearing anymore.

`deploy/Dockerfile.api` installs `postgresql-client-17` from the PGDG repo in
the image itself (pinned to major 17, matching the `postgres:17-alpine`
spine image). The rule that matters is unchanged even though where it's
enforced moved: the client's major version must be **at least** the server's
— an older `pg_dump` refuses a newer server outright. A compose bump to
`postgres:18` now requires bumping `Dockerfile.api`'s PGDG install in the
same change, not a host `apt-get`.

```bash
sudo apt-get install -y postgresql-client-17
```

The host package above is no longer load-bearing for backups — kept only if
you want `psql`/`pg_dump` available for ad-hoc use directly on the host (e.g.
against the loopback-published `127.0.0.1:5432`). It has no bearing on
whether `bellasreef backup`/`restore` work; those go through the image.

# Host setup

The **only** mutations made to the Pi outside containers. Everything else the
platform needs runs in Docker with specific device nodes passed to `hardware-io`
and nothing privileged.

Keep this file exhaustive. If a host change is needed and it is not written
here, that is a bug in this document, not a licence to make the change quietly.

Values below are the measured state of the reference host as of 2026-08-09; see
"Verified host facts" in CLAUDE.md for the full inventory.

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

Alongside the dtoverlays above, `/home/david/bellasreef/deploy/.env` is host
state, not repo state: it is gitignored (`deploy/.env.example` is the
template committed instead) and holds the values `bellasreef-spine.service`
interpolates into `deploy/compose.yaml` — `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`, `BELLASREEF_DATABASE_URL`, `NATS_URL`,
`VM_RETENTION`, `I2C_GID`, `GPIO_GID`.

`I2C_GID`/`GPIO_GID` are required in this file even though the spine service
starts no container with hardware access — compose interpolates the whole
file before evaluating which services `up` targets, so a missing variable
here fails the spine's `docker compose up`, not just an app container that
never runs.

Never committed, and never reset by `deploy-pi.sh` — a git reset touches the
repo clone, not this file. Verify it exists and is current after any change
to Postgres credentials, retention, or the host's `i2c`/`gpio` group GIDs
(`getent group i2c gpio`).

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

## 7. Services under systemd, and the restart drill

Every service on this host runs as a supervised systemd unit, from a pushed
commit. There are no dev launchers; `scripts/dev/run-*.sh` were deleted on
2026-08-11 after an unsupervised hardware-io exited and stayed exited for ten
hours. See CLAUDE.md, "Deployment discipline".

Deploy with `scripts/deploy-pi.sh`, which refuses a dirty or unpushed tree,
resets `/home/david/bellasreef` to the pushed commit, installs the units,
restarts them, and then waits for a fresh sample to reach VictoriaMetrics
before reporting success.

### Service configuration lives on the host

The unit files carry no environment values, so the same unit works on any hub.
Configuration is read from:

```
/etc/bellasreef/hardware-io.env
/etc/bellasreef/control-engine.env
/etc/bellasreef/api.env
```

Mode `0640`, group `david`. They hold the database DSN, which contains a
password, and `Environment=` lines in a unit file are readable by any user
through `systemctl show`.

These files are deliberately not in the archive `bellasreef backup` writes, so
on fresh hardware they are authored by hand. The full `api.env`, as the live
hub runs it — the password is the one you put in `deploy/.env` (§1b):

```
BELLASREEF_NATS_URL=nats://localhost:4222
BELLASREEF_DATABASE_URL=postgresql+asyncpg://bellasreef:<password>@localhost:5432/bellasreef
BELLASREEF_VM_URL=http://localhost:8428
BELLASREEF_LOG_LEVEL=INFO
BELLASREEF_ASSUME_CLOCK_TRUSTED=1
```

`BELLASREEF_VM_URL` is not optional decoration: `bellasreef backup` refuses to
run without it (or an explicit `--no-telemetry-snapshot`), so an `api.env`
missing it breaks backups, not just dashboards. The other two service files
carry the same DSN/NATS pair minus the VM URL.

`BELLASREEF_NATS_URL` is the entry that bites. Leave it out and hardware-io
reads the probe, serves metrics, logs a clean startup, and publishes nothing at
all. Nothing about the process looks wrong; the tank is simply not monitored.
That is why `deploy-pi.sh` verifies a sample on the wire instead of a process
in the table.

### Logs are in journald

```bash
journalctl -u bellasreef-hardware-io -f
journalctl -u bellasreef-hardware-io --since "2 hours ago"
```

Not `/tmp/*.log`. A log in `/tmp` is truncated by the next start, which is how
the evidence for the first half of the 2026-08-10 outage was destroyed before
anyone read it.

### Installing by hand

`deploy-pi.sh` does this for you; the manual form is here for a first
bring-up.

```bash
sudo install -m 0644 deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bellasreef-spine
sudo systemctl enable --now bellasreef-hardware-io bellasreef-control-engine bellasreef-api
```

The spine first: the app units crash-loop (harmlessly, `Restart=always`) until
NATS and Postgres answer, and on a machine being *restored* rather than
deployed, `docs/backup-restore.md` needs the spine up steps before the app
units exist to start.

The unit is ordered `After=time-sync.target` / `Wants=time-sync.target`. On a
board with no RTC battery that ordering is the difference between a scheduler
starting against a real clock and one starting against whatever `fake-hwclock`
restored. It only means anything because `chrony-wait` is enabled (§3) — without
it, `time-sync.target` is reached as soon as the NTP daemon starts, which is not
the same claim.

### Two runtimes, two liveness mechanisms

CLAUDE.md locks Docker Compose as the runtime, and **Docker does not restart a
container that is merely unhealthy** — `restart: unless-stopped` acts on process
*exit*. A healthcheck alone therefore cannot recover a hung event loop in a
container; something has to make the process die.

So the service carries both:

| Runtime | Mechanism | Recovery |
|---|---|---|
| systemd | `sd_notify WATCHDOG=1` from inside the supervisor loop | systemd SIGABRTs and restarts on `WatchdogSec` |
| Docker | `LivenessGuard` thread calling `os._exit` | process exits, `restart: unless-stopped` brings it back |

The guard runs on a **thread**, not an asyncio task, because a frozen event loop
cannot run the task that would have rescued it.

### Telling the death modes apart

The liveness guard exits **70** (`EX_SOFTWARE`) deliberately, so a post-incident
`docker inspect` or journal entry says which mechanism fired:

| Exit | Meaning |
|---|---|
| `0` | clean stop |
| `70` | **liveness guard — the supervisor loop stalled** |
| `134` | systemd watchdog SIGABRT (128+6) |
| `137` | SIGKILL / OOM killer (128+9) |

`docker inspect --format '{{.State.ExitCode}}' bellasreef-hardware-io-1`

A stall and an OOM kill send you to completely different places, and at 3 a.m.
the exit code is the fastest way to know which.

### Metrics are not published to the host

`hardware-io` uses `expose:` rather than `ports:`. Health and metrics ride the
compose network only — victoria-metrics scrapes `http://hardware-io:9101/metrics`
from inside it. The one container holding `/dev` should not also own a listening
socket on the host.

### Running the drill

```bash
./scripts/drill-restart.sh reef        # from the dev machine
```

It needs the freeze trigger armed, which is deliberately opt-in — an always-armed
"hang yourself" signal in production is a liability:

```bash
sudo mkdir -p /etc/systemd/system/bellasreef-hardware-io.service.d
printf '[Service]\nWatchdogSec=10\nEnvironment=BELLASREEF_ENABLE_FREEZE_DRILL=1\n' \
  | sudo tee /etc/systemd/system/bellasreef-hardware-io.service.d/drill.conf
sudo systemctl daemon-reload && sudo systemctl restart bellasreef-hardware-io
```

The drill asserts three things, and the second is the one that matters:

1. systemd attributed the kill to **`Watchdog timeout`**, not something incidental.
2. the restarted process **re-ran the startup safe-state assertion** — the drill
   actuator deliberately comes up *energised*, so a restart that skipped the
   assertion would be visible rather than silently passing.
3. the health endpoint is green again.

Verified 2026-08-09: freeze at 23:50:14.797, watchdog timeout at 10 s, SIGABRT,
restart, safe state re-asserted at 23:50:25.628 — about 10.8 s from hung loop to
hardware safe.

**Remove the drop-in when finished.** Leaving the freeze trigger armed on a
tank controller means one stray signal hangs it until the watchdog fires.

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
# In /boot/firmware/config.txt, [pi5] section. Applied and verified 2026-08-12.
dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
```

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

Before the overlay, `pwmchip0` **was** `1f0009c000.pwm`. The index is not stable
across a config change, exactly like `gpiochip*`. Our bindings target
`pwmchip0`, which is correct today and is worth re-checking after any overlay
edit.

Verify with `pinctrl get 12,13` after a reboot; a channel that exports happily
while the pin reads `none` is the failure this section exists to prevent.

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

### Running the CLI

`bellasreef` is a console script installed with the API package, so it lives in
the same virtualenv systemd starts uvicorn from. It talks to Postgres directly
and does not go through the API, which is the point: the API is what you cannot
authenticate to.

Configuration comes from the environment, and the service's environment lives in
`/etc/bellasreef/api.env` (section 7), not in the unit file. Source it:

```bash
cd /home/david/bellasreef
set -a; . /etc/bellasreef/api.env; set +a
.venv/bin/bellasreef revoke --list
```

| Variable | What it is for | Missing |
|---|---|---|
| `BELLASREEF_DATABASE_URL` | everything. Required. | refuses, exit 2 |
| `BELLASREEF_NATS_URL` | publishing the audit event | the command still does its job, then warns on stderr that the event was **not** recorded |

Sourcing the file gets you both. Exporting the DSN by hand and nothing else is
the mistake that loses the audit row, which is why the warning is loud rather
than a log line.

### Listing clients

```bash
.venv/bin/bellasreef revoke --list
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
.venv/bin/bellasreef revoke 0f9e4c6a-1d2b-4a77-9f3e-1c5b8a2d4e60
.venv/bin/bellasreef revoke "David's iPhone"
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
cd /home/david/bellasreef
set -a; . /etc/bellasreef/api.env; set +a

# 1. Open a recovery window. Default 300 seconds; --ttl takes seconds.
.venv/bin/bellasreef pair --ttl 600

# 2. On the replacement phone, open the app, pick the hub, pair.
#    It gets a token immediately instead of a six-digit code, because the
#    window is open. The window is spent by whoever uses it first.

# 3. Turn the lost phone off.
.venv/bin/bellasreef revoke --list
.venv/bin/bellasreef revoke <id of the lost phone>
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

## 11. PostgreSQL client tools — for `bellasreef backup`

`bellasreef backup` and `bellasreef restore` spawn `pg_dump`/`pg_restore` as
host binaries. Postgres itself runs in a container, but its copy of the tools
cannot be borrowed: `pg_dump --file` writes inside whatever filesystem the
binary runs in, so a `docker exec` detour leaves the dump in the container, not
in the archive. The host needs its own client package:

```bash
sudo apt-get install -y postgresql-client-17
```

Debian ships one PostgreSQL major per release and the compose spine pins
`postgres:17`, so today the two coincide — but nothing couples them. The rule
that matters: the client's major version must be **at least** the server's — an
older `pg_dump` refuses a newer server outright. A compose bump to
`postgres:18` therefore requires installing the matching newer client on the
host in the same change, or every subsequent backup fails with that refusal
until someone notices. Installed and verified on this host 2026-08-12 (client
17.10, server 17.10; backup + restore drill both passed against the live
spine).

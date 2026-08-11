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
sudo systemctl enable --now bellasreef-hardware-io bellasreef-control-engine bellasreef-api
```

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

## 9. The device file, and the PWM overlay

### `/etc/bellasreef/devices.yaml`

hardware-io is topology-driven. What this hub is wired to is declared in
`/etc/bellasreef/devices.yaml`, host-owned like the service env files beside
it, so the code and the unit files stay identical on every hub.

```bash
sudo install -m 0644 deploy/config/devices.yaml.example /etc/bellasreef/devices.yaml
sudo -e /etc/bellasreef/devices.yaml
```

The service **refuses to start** on a missing, malformed, or unknown-driver
entry, and names the offending device. Coming up with a light missing is a hub
that looks healthy and monitors half a tank, which is the failure this project
keeps meeting in different costumes.

Override the path for testing with `BELLASREEF_DEVICES_FILE`.

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
# In /boot/firmware/config.txt. Two single-channel overlays, not pwm-2chan —
# this is the form the archived HAL (v3.1.0) shipped and ran on real hardware.
dtoverlay=pwm,pin=12,func=4
dtoverlay=pwm,pin=13,func=4
```

Channel mapping, from the archive: **channel 0 → GPIO12, channel 1 → GPIO13.**

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

After editing, reboot and confirm the channel exports:

```bash
echo 0 | sudo tee /sys/class/pwm/pwmchip0/export
ls /sys/class/pwm/pwmchip0/pwm0/     # period duty_cycle polarity enable
echo 0 | sudo tee /sys/class/pwm/pwmchip0/unexport
```

`/sys/class/pwm` is **not** the forbidden sysfs GPIO interface. `/sys/class/gpio`
is deprecated and absent on this board; `/sys/class/pwm` is the current kernel
PWM ABI and the only interface the RP1 PWM block exposes. libgpiod does not
cover PWM — it is a GPIO character-device library, and hardware PWM is not GPIO.

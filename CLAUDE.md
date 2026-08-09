# CLAUDE.md — Bella's Reef

Reef aquarium automation platform. Bella's Reef LLC. Production-grade from first commit.

**Document authority:** On any conflict between this file and `docs/prd.md`, the PRD wins. Flag the conflict to the operator instead of resolving it silently. The "Verified host facts" section at the bottom is the exception — it is ground truth measured on the target hardware and overrides assumptions anywhere else.

## Non-negotiable principles

1. **Build once, build right.** Every component is the production choice. No staging-grade stand-ins, no "swap later," no TODO-driven architecture. If a decision below says PostgreSQL, do not prototype with SQLite. Ever.
2. **Modern floor.** Linux 6.x+, Pi 5+ arm64, Python 3.13+, iOS 26+, current stable toolchains. Zero legacy accommodation. Deprecated APIs (sysfs GPIO, RPi.GPIO) are forbidden.
3. **Safety is architecture.** Every actuator declares safe state, max runtime, heartbeat timeout at registration — or registration is rejected. Interlocks enforced in hardware-io, below control logic. No control loop actuates real hardware until fail-safe drills pass.
4. **API-first.** All clients consume the versioned OpenAPI contract. Nothing reaches around the API. No undocumented endpoints, ever.
5. **Never guess, never fabricate.** Unknown register map, unclear kernel interface, uncertain library behavior → say so and verify against docs/hardware. A confident wrong answer is worse than no answer.

## Locked stack (do not revisit, do not substitute)

| Layer | Choice |
|---|---|
| Relational | PostgreSQL 17, Alembic migrations from schema v1 |
| Time-series | VictoriaMetrics |
| Message spine | NATS + JetStream (durable commands w/ expiry; core pub/sub for telemetry) |
| API | FastAPI + Pydantic v2, OpenAPI-first |
| Runtime | Docker Compose, pinned multi-arch images (arm64 + amd64), least-privilege |
| GPIO/I2C | libgpiod v2 / kernel char device only |
| Auth | OAuth2/JWT from the first endpoint |
| Logs/metrics | Structured JSON logs + Prometheus-format metrics from every service |
| iOS | Swift/SwiftUI, iOS 26+, client generated via swift-openapi-generator — no hand-written bindings |
| Web UI | Standalone SPA container, same API + WebSocket as iOS |

## Architecture

Five services + spine, all containers. Hub-and-spoke ready; phase 1 is single Pi 5.

- `hardware-io` — SOLE owner of `/dev/gpiochip*`, `/dev/i2c-*`, 1-wire, serial. Exposes devices over NATS subject schema. Knows nothing about reef logic. Enforces actuator interlocks and heartbeat-loss safe-state locally.
- `control-engine` — all control loops, scheduling, interlock supervision. Sole command publisher to actuators. Never lives inside the API process.
- `api` — stateless FastAPI front door. REST + WebSocket bridge to NATS. Publishes commands, subscribes state.
- `postgres` — config, dosing journal (transactional: intent→execution→confirmation), calibration, append-only audit log.
- `victoria-metrics` — all telemetry + derived metrics.

## Versioned contracts (semver; breaking change = migration)

1. NATS subject schema + Pydantic payload models: `bellasreef.sensor.<type>.<id>`, `bellasreef.cmd.<class>.<id>`, `bellasreef.state.<id>`. This is what phase-2 ESP32 spokes will speak — a spoke joins with ZERO changes to engine/api/clients.
2. OpenAPI spec — published artifact, source of truth for all clients.
3. Hardware driver interface — stable even with one implementation. Sensor reads are async with per-driver polling cadence in the interface (DS18B20 blocks ~750ms/read at 12-bit; the serialized w1 bus must never dictate loop timing).

## Build order (dependency-gated, do not reorder)

1. Contracts: subject schema, driver interface, OpenAPI skeleton, Postgres schema v1 + Alembic. CI + multi-arch builds BEFORE feature code.
2. Spine + safety: hardware-io w/ safety framework + Day-1 drivers (PCA9685 PWM dimming over I2C, DS18B20 1-wire temp). Fail-safe drills (process kill, container kill, NATS outage, power pull) pass before any control logic.
3. Day-1 vertical: lighting schedules + temp monitoring end-to-end (driver→NATS→engine→VM→API→clients). Lowest-risk actuator first: a dimmer failing safe is a dark tank; a heater failing unsafe is a dead one.
4. Control modules individually, each entering service via shadow mode (decisions journaled, zero actuation). Temperature CONTROL waits for relay drivers + passed drills.
5. Clients, hardening, publication.

## Dev environment

- Dev machine: macOS. Project root: this repo.
- Target: Raspberry Pi 5, NVMe (SD unsupported), Raspberry Pi OS/Debian arm64, kernel 6.x+. Reachable via `ssh <pi-host>` (see `.env.local`, never committed).
- Workflow: code + unit tests on Mac; hardware-dependent integration tests run on the Pi via SSH. Images built multi-arch; deploy = `docker compose pull && up -d` on the Pi.
- Hardware access in containers: pass specific `/dev` nodes to hardware-io only. No privileged containers.
- Host-touching config (dtoverlays: w1-gpio, i2c enable) is documented in `docs/host-setup.md` as the ONLY host mutation.

## Code standards

- Python 3.13+, fully typed, `mypy --strict` clean. Ruff for lint/format.
- Pydantic v2 models for every message payload and API schema. One source of truth per model.
- Tests: pytest; contract tests for NATS payloads and OpenAPI; hardware drivers get a fake implementation of the driver interface for engine tests.
- Commits: conventional commits. PRs must pass CI (lint, types, tests, multi-arch build) — no direct pushes to main once CI exists.
- Every service: healthcheck endpoint, structured JSON logs, metrics endpoint. Day 1, not later.

## Things Claude must not do

- Substitute any locked stack choice "temporarily."
- Write API clients by hand (they are generated).
- Put control logic in the API service or hardware knowledge in the control engine.
- Use sysfs GPIO, RPi.GPIO, or any deprecated kernel interface.
- Register an actuator without safe state + max runtime + heartbeat timeout.
- Invent register maps, pinouts, or library behavior. Verify or ask.

---

# Verified host facts

Measured directly on the target hardware on 2026-08-09. Ground truth — overrides
assumptions anywhere else in this file or the PRD. Re-measure before trusting any
of it after a hardware or OS change.

## Machine

```
board     Raspberry Pi 5 Model B Rev 1.0, 8 GB
os        Debian GNU/Linux 13 (trixie), aarch64
kernel    6.18.39+rpt-rpi-2712
python    3.13.5 (PEP 668 externally-managed — system pip blocked)
hostname  bellasreef
rootfs    /dev/sda2 ext4 rw,noatime — 115 GB SanDisk Ultra Fit, USB-attached
boot      /dev/sda1 vfat, mounted at /boot/firmware
```

The operating user is `david`, in groups `gpio i2c spi dialout docker`. Hardware
access requires no elevation; services must not run as root.

## GPIO topology

The 40-pin header is the **RP1** southbridge:

```
gpiochip0   pinctrl-rp1              54 lines   <- the 40-pin header
gpiochip*   gpio-brcmstb@...         17/6/32/4 lines — internal SoC, NOT header pins
```

The `/dev/gpiochipN` index has moved between kernel releases — resolve by label
(`pinctrl-rp1`), never hardcode the number.

## Bus and device paths

| Interface | Path | State |
|---|---|---|
| I²C | `/dev/i2c-1` | enabled, bus scanned clean `0x03–0x77`, nothing wired yet |
| I²C (HDMI DDC) | `/dev/i2c-13`, `/dev/i2c-14` | ignore |
| 1-Wire | `/sys/bus/w1/devices/w1_bus_master1` | live; DS18B20 probes appear as `28-*` |
| SPI | — | disabled (`dtoverlay=nospi10`) |
| Watchdog | `/dev/watchdog0` | enabled by default; systemd `RuntimeWatchdogUSec=1min` |

**The 1-Wire read path is sysfs** (`/sys/bus/w1/devices/28-*/w1_slave`). That is
the only kernel interface the `w1-therm` driver exposes — there is no character
device equivalent. This is distinct from the forbidden *sysfs GPIO*
(`/sys/class/gpio`), which is deprecated and unavailable on this board anyway.

**Pi 5 overlay name:** 1-Wire requires `dtoverlay=w1-gpio-pi5`, not `w1-gpio`.
The plain overlay is the pre-Pi-5 variant and silently fails to bring up the bus.
Current setting: `dtoverlay=w1-gpio-pi5,gpiopin=4` in the `[pi5]` section.
DS18B20 DATA needs a physical 4.7 kΩ pull-up to 3V3; the overlay's internal
pull-up is too weak for tank-length probe cables.

## Boot configuration

`/boot/firmware/config.txt` is stripped for headless operation: I²C and 1-Wire on,
SPI off, all audio off, **no display stack** (`vc4-kms-v3d` removed — 0 `vc4` and
0 `snd` modules loaded), ACT LED on `heartbeat`. Timestamped backups sit beside it
as `config.txt.bak-*`.

Never put a trailing `#` comment on a `dtparam=`/`dtoverlay=` line — the firmware
DT parser can fold it into the value.

Bootloader EEPROM: May 2026. `BOOT_ORDER=0xf146` — reads right-to-left as NVMe →
USB mass storage → SD → **loop forever**, so a boot device that isn't ready yet
gets retried rather than halting.

With no display stack, the **serial console is the only live out-of-band console**:
`serial-getty@ttyAMA10` is running and `console=serial0,115200` is in `cmdline.txt`
(USB-TTL adapter on the 3-pin debug connector). Offline recovery is to power off,
pull the USB drive, and mount `/dev/sda1` (vfat) on a Mac.

## Clock — no RTC battery

**There is no RTC battery fitted.** A power cut loses the time. Three layers are
installed and enabled to compensate:

| Unit | Role |
|---|---|
| `fake-hwclock-load` / `-save` + timer | restores saved time at boot, before any network |
| `chrony` | corrects once the network is up (`makestep 1 3` steps rather than slews) |
| `chrony-wait` | makes `time-sync.target` mean *actually synchronised* |

`chrony` replaced `systemd-timesyncd`. Timezone is `America/Los_Angeles`, which
observes DST. `fake-hwclock.service` and `hwclock.service` both show as `masked` —
that is the package masking its own legacy sysvinit units, not a fault.

Any time-driven actuation must be ordered `After=time-sync.target` /
`Wants=time-sync.target` and treat an untrusted clock as a fault state.

## Network

Interfaces are DHCP and **the WiFi address changes** — resolve by name.
`avahi-daemon` publishes `bellasreef.local`, which is the same Bonjour mechanism
iOS uses natively.

`/etc/avahi/avahi-daemon.conf` pins `allow-interfaces=eth0,wlan0`. Without it,
avahi also advertised Docker's `docker0` bridge (`172.17.0.1`), which is
unreachable from the LAN. An allowlist is used rather than denying `docker0`
because Docker also creates `br-*` bridges and avahi has no wildcard syntax — so
if `bellasreef.local` ever stops resolving, check this file first.

WiFi power save is **off**, persisted in the NetworkManager connection profile.
No firewall is active (`nftables`/`ufw` both inactive).

## Installed tooling

`git` 2.47.3 · `docker` 29.7.2 · `uv` 0.12.3 · `libgpiod` 2.2.1 · `i2c-tools` 4.4 ·
`iw` 6.9 · `rsync` 3.4.1 · `chrony` 4.6.1 · `pinctrl`

**PATH trap:** `i2cdetect`, `iw`, and `hwclock` live in `/usr/sbin`, which is not
on `PATH` for non-interactive SSH. `ssh <pi> i2cdetect` reports "not found" even
though the tool is installed — prepend `/usr/sbin:/sbin` or use absolute paths.
`uv` is symlinked into `/usr/local/bin` so systemd units can find it.

## Readiness check

```bash
ssh <pi-host> 'export PATH=$PATH:/usr/sbin:/sbin
  ls /sys/bus/w1/devices/                       # w1_bus_master1 + any 28-* probes
  i2cdetect -y 1
  gpiodetect | grep rp1                         # gpiochip0 [pinctrl-rp1] (54 lines)
  timedatectl show -p NTPSynchronized --value   # must be yes before scheduling
  systemctl is-active time-sync.target
  iw dev wlan0 get power_save                   # must be off
  rpi-eeprom-config | grep BOOT_ORDER           # 0xf146
  vcgencmd get_throttled                        # 0x0
  lsmod | grep -c "^snd\|^vc4"                  # 0 — no display/audio stack
'
```

# CLAUDE.md — Bella's Reef

Reef aquarium automation platform. Bella's Reef LLC. Production-grade from first commit.

**Document authority:** On any conflict between this file and `docs/prd.md`, the PRD wins. Flag the conflict to the operator instead of resolving it silently. The "Verified host facts" section at the bottom is the exception — it is ground truth measured on the target hardware and overrides assumptions anywhere else.

## Non-negotiable principles

1. **Build once, build right.** Every component is the production choice. No staging-grade stand-ins, no "swap later," no TODO-driven architecture. If a decision below says PostgreSQL, do not prototype with SQLite. Ever.
2. **Modern floor.** Linux 6.x+, Pi 5+ arm64, Python 3.13+, iOS 26+, current stable toolchains. Zero legacy accommodation. Deprecated APIs (sysfs GPIO, RPi.GPIO) are forbidden.
3. **Safety is architecture.** Every actuator declares its control authority; an authoritative one additionally declares safe state, max runtime and heartbeat timeout — or registration is rejected. Interlocks enforced in hardware-io, below control logic. No control loop actuates real hardware until fail-safe drills pass.
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

## Environment boundary (non-negotiable)

**Integration tests never connect to the hub's broker or database.** Not as a
habit — structurally. `conftest.py` refuses to run when any `BELLASREEF_TEST_*`
endpoint resolves to anything but loopback, or when the target database is the
production `bellasreef`.

This is not caution, it is arithmetic. Durables are shared broker state and
BR_CMD is a workqueue that permits exactly one consumer per filter subject, so
a test that binds a durable on the hub's NATS is *contending for the hub's own
slot by design*. A test run cost ten hours of lost monitoring on 2026-08-10:
a leaked `doomed-<uuid>` durable held the filter, hardware-io could not
re-bind, and the process exited.

Local integration runs use ephemeral dev containers on loopback, the same
shape CI uses. With no container runtime installed, the integration suites do
not run — say so explicitly with `BELLASREEF_ALLOW_ENV_SKIPS=1` and let CI be
the place they are actually checked. Pointing them at the hub is not an
alternative.

## Deployment discipline (non-negotiable)

**The Pi runs the whole stack as supervised containers, from pushed commits,
only.** All five services — nats, postgres, victoria-metrics, hardware-io,
control-engine, api — are containers under one boot unit,
`bellasreef.service`; Docker's own restart policies (`restart:
unless-stopped`) are the supervisor from there on. Containers-only per the
PRD topology, ruled by David 2026-08-13 ("containers only — we are not
carrying that decision").

- No dev launchers. `scripts/dev/run-*.sh` are deleted, not deprecated. An
  unsupervised process that exits stays exited, which is the second half of the
  same 2026-08-10 outage.
- No rsync, no editing on the Pi, no uncommitted state. `/home/david/bellasreef`
  is a git clone reset to `origin/main`; anything not pushed does not run.
- Deploy with `scripts/deploy-pi.sh`. It refuses a dirty or unpushed tree,
  resets the Pi to the pushed commit, pulls the three app images by
  digest-verified SHA tag, applies migrations via a one-off `docker compose
  run --rm api`, recreates hardware-io/control-engine/api, and then
  **verifies fresh telemetry on the wire** before reporting success. A
  process check is not a deploy check: hardware-io without
  `BELLASREEF_NATS_URL` starts cleanly, serves metrics and publishes nothing.
- **A backend pass is not done at CI green.** The stop condition is
  **CI green → `scripts/deploy-pi.sh` → telemetry verified on the wire.** All
  three, every time.

  Green-and-undeployed is a state that reads as finished and is not. The Pi
  drifted four commits behind main that way in one session, which meant the hub
  was serving a contracts version it did not have and running code whose
  replacement had already been reviewed and merged. "It passed CI" describes a
  runner in a datacentre; the tank is on a shelf.
- Service configuration is no longer `/etc/bellasreef/<service>.env` — that
  era is closed (see the dated paragraph below). Config now flows through
  `deploy/.env` (compose interpolation: `POSTGRES_*`, `BELLASREEF_TAG`,
  `VM_RETENTION`, `I2C_GID`/`GPIO_GID`, `BELLASREEF_DATABASE_URL`, `NATS_URL`)
  and the `environment:` blocks in `deploy/compose.yaml` itself. One file plus
  one committed manifest, not a per-service directory on the host.
- Logs are `docker compose logs -f <service>` (or `docker logs
  bellasreef-hardware-io-1`), not journald-per-unit. A log in `/tmp` gets
  overwritten by the next start, which destroyed the evidence for the first
  half of the 2026-08-10 outage — the same reasoning, now pointed at compose's
  own log driver instead of journald.
- Spine data services (postgres, nats, victoria-metrics) are never
  force-recreated by a deploy: they keep `restart: unless-stopped` and their
  compose definitions don't change per-deploy, so `docker compose up -d` only
  touches the three app services. `bellasreef.service` is only ever `start`ed
  by a deploy, never `restart`ed — restarting it would `up -d --wait` the
  whole stack, including the data services, recreating the exact
  durable-contention risk the environment boundary rule above exists to
  prevent.
- A fresh registry means no devices. After any factory wipe, sensors must be
  re-imported (`docker compose exec api bellasreef devices import
  /etc/bellasreef/devices.import.yaml`, which needs an API token via the
  TOFU-pair-a-seed-CLI-then-revoke dance) before the deploy telemetry gate can
  pass — no devices, no readings, no wire traffic to verify. The 2026-08-12
  cutover hit exactly this.

**Closed detour, dated: the host-systemd-units era.** Between the 2026-08-10
outage and 2026-08-13, services ran as three separate systemd app units
(`bellasreef-{hardware-io,control-engine,api}.service`) reading
`/etc/bellasreef/<service>.env`, alongside a `bellasreef-spine.service` that
brought up only the data containers — an overcorrection from the outage that
put supervision on the host rather than in the runtime CLAUDE.md already
locked. Removed 2026-08-13 per the PRD's containers-only topology and David's
ruling; the app units are deleted from the host and from this repo, not kept
around "just in case." Anything below referencing that era is describing a
closed chapter, not current practice.

- RESOLVED (topology, 2026-08-13): containers won. All five services run
  containerized under one boot unit; the host-systemd-app-units alternative
  is deleted, not deferred.
- RESOLVED (LAN exposure, 2026-08-13): closed by David's ruling not to rotate
  credentials — the three-day exposure of the ad-hoc `-dev` spine
  (0.0.0.0:5432/4222/8428, 2026-08-09 through cutover) is accepted as a past
  window with nothing known to have used it, not remediated after the fact.
- RESOLVED (unit ordering, 2026-08-13): obsolete by construction — there is
  one boot unit now, not several to order against each other, and
  `depends_on: { condition: service_healthy }` in compose.yaml handles
  intra-stack sequencing (e.g. hardware-io after nats).

## Bench boundary (non-negotiable)

**At the bench, Claude's role is registers and commands. Nothing else.**

- In scope: set the requested duty, write the requested register, read it back,
  report exactly what was written and exactly what was read. Show every command
  before running it.
- **Out of scope: electrical design.** Voltages, absolute-maximum ratings,
  wiring, pull-ups, component choices, and what a load will do with a signal are
  not Claude's to reason about, advise on, or verify. Those facts arrive as
  David's measurements and rulings, and are recorded in "Verified host facts"
  **as given** — not derived, not sanity-checked, not improved.
- If a step looks electrically wrong: **one sentence, then stop.** No analysis,
  no alternatives, no proposed redesign. Flagging is the whole contribution.

This exists because the failure modes are asymmetric. A confident chain of
electrical reasoning from a model that has never seen the bench reads exactly
like knowledge, and the cost of being wrong is measured in hardware and
livestock. A one-line flag costs a pause.

Code comments follow the same rule: a driver may record *what was ruled* and
*what was measured*, and must not argue the physics.

## Dev environment

- Dev machine: macOS. Project root: this repo.
- Target: Raspberry Pi 5, NVMe (SD unsupported), Raspberry Pi OS/Debian arm64, kernel 6.x+. Reachable via `ssh <pi-host>` (see `.env.local`, never committed).
- Workflow: code + unit tests on Mac; integration tests against loopback dev containers or in CI, never against the hub. Deploy with `scripts/deploy-pi.sh`.
- Hardware access in containers: pass specific `/dev` nodes to hardware-io only. No privileged containers.
- Host-touching config (dtoverlays: w1-gpio, i2c enable) is documented in `docs/host-setup.md` as the ONLY host mutation.

## Code standards

- Python 3.13+, fully typed, `mypy --strict` clean. Ruff for lint/format.
- Pydantic v2 models for every message payload and API schema. One source of truth per model.
- Tests: pytest; contract tests for NATS payloads and OpenAPI; hardware drivers get a fake implementation of the driver interface for engine tests.
- **Integration tests must delete every durable they create.** A test that leaves the system broken is as bad as one that silently skips. A JetStream workqueue stream permits exactly one consumer per filter subject, so a leaked durable does not merely litter — it stops the real service from ever binding again. This has cost three separate debugging detours; both known leaks (`ramp-<uuid>` on BR_CMD, a test-scoped audit durable on BR_AUDIT) now clean up on teardown.
- A test skipped for a missing environment **fails** the gate (`conftest.py`). Skips are decisions, declared with `BELLASREEF_ALLOW_ENV_SKIPS=1`, never incidental.
- Commits: conventional commits. PRs must pass CI (lint, types, tests, multi-arch build) — no direct pushes to main once CI exists.
- Every service: healthcheck endpoint, structured JSON logs, metrics endpoint. Day 1, not later.

## Things Claude must not do

- Substitute any locked stack choice "temporarily."
- Write API clients by hand (they are generated).
- Put control logic in the API service or hardware knowledge in the control engine.
- Use sysfs GPIO, RPi.GPIO, or any deprecated kernel interface.
- Register an `authoritative` actuator without safe state + max runtime + heartbeat timeout — or accept a declared safe state on an `advisory` or `observe_only` one. A safe state we cannot enforce must be rejected, never stored and ignored. See `docs/device-classes.md` §2.
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
| I²C | `/dev/i2c-1` | enabled; see device inventory below |
| PWM (RP1) | resolve by device identity: `1f00098000.pwm` (PWM0, ours) vs `1f0009c000.pwm` (PWM1, fan) — the pwmchipN index moves between kernels. npwm 4, all four pin-muxed by `pwm-4chan` since 2026-08-13 |
| I²C (HDMI DDC) | `/dev/i2c-13`, `/dev/i2c-14` | ignore |
| 1-Wire | `/sys/bus/w1/devices/w1_bus_master1` | live; DS18B20 probes appear as `28-*` |
| SPI | — | disabled (`dtoverlay=nospi10`) |
| Watchdog | `/dev/watchdog0` | enabled by default; systemd `RuntimeWatchdogUSec=1min` |

### Attached device inventory (verified 2026-08-09, real reads)

| Device | Address | Verified |
|---|---|---|
| DS18B20 temp probe | 1-Wire ROM `28-000000bfe244` | 25.187 °C, `crc=YES`, 3/3 stable samples |
| PCA9685 16-ch PWM | I²C `0x40` (+ all-call `0x70`) | MODE1 `0x11`, PRE_SCALE `0x1e` |

`0x70` on a bus scan is **not a second board** — it is the PCA9685's ALLCALLADR
(`0x05` reads `0xE0`; `0xE0 >> 1 = 0x70`). Expect both addresses from one chip.

**PWM: all four RP1 PWM0 channels, verified live 2026-08-13.** The archive's
"two channels" claim was the Pi-4 era talking; the RP1's PWM0 block has four
independent channels and every one reaches a header pin on this board:

| Channel | GPIO | Header pin | Legacy `brcm,function` | RP1 alt |
|---|---|---|---|---|
| PWM0_CHAN0 | 12 | 32 | 4 | a0 |
| PWM0_CHAN1 | 13 | 33 | 4 | a0 |
| PWM0_CHAN2 | 18 | 12 | **2** | a3 |
| PWM0_CHAN3 | 19 | 35 | **2** | a3 |

All four measured muxed with `pinctrl get 12,13,18,19` after the custom
`pwm-4chan` overlay (see `docs/host-setup.md` §9 — built on the Pi with `dtc`;
current `config.txt` runs `dtoverlay=pwm-4chan`). The legacy function values
are NOT the RP1 alt numbers: 12/13 take `4` (legacy ALT0) but 18/19 take `2`
(legacy ALT5, the BCM-era PWM position — the compat layer translates per-pin).
`func=7` on 18/19 is rejected as `invalid function` and **poisons the whole
pin map**, unmuxing 12/13 too. The archive's two-separate-`dtoverlay=pwm`-lines
form also fails here (only the second takes). A channel that exports in sysfs
while its pin reads `none` is the standing trap; check `pinctrl get`, never
the presence of a sysfs directory — which is exactly what hardware-io now does:
discovery shells `pinctrl get` and announces only what the mux proves, so an
overlay change is reflected at the next service start with zero code change.

The two RP1 PWM instances: `1f00098000.pwm` is PWM0 (ours); `1f0009c000.pwm`
is PWM1 (fan header). The pwmchip index has moved between kernels — resolve by
device identity, which discovery also now does. `pinctrl` lives in `/usr/bin`
(verified `command -v`; a plan once pinned `/usr/sbin` unchecked and discovery
failed at the next boot — loudly, by design).

**Rule, from how this was learned:** a recorded measured-vs-documented
discrepancy (like npwm=4 vs the archive's 2, which sat unresolved under two
days of PWM work) is a **blocking flag** — no dependent config or unit ships
on top of it until it is resolved on hardware or explicitly accommodated in
the design.

**Measured DS18B20 read cost: 831 ms** on this hardware, above the ~750 ms
datasheet conversion time. This is the empirical basis for the driver-interface
timing rule — the cost is real, it is per-probe, and the bus is serialized.

**PCA9685 power-on state:** MODE1 `0x11` = SLEEP set, oscillator off, no PWM
being generated — safe idle. PRE_SCALE `0x1e` (30) is the default ≈196 Hz.
MODE2 `0x04` = **OUTDRV set, i.e. totem-pole output**, which as of the
2026-08-11 ruling is what we want — the PCA9685 drives an external N-FET gate
(item 0a). An earlier plan called for open-drain; it is withdrawn, and item 0
says why. Do not assume the default is what we want either way: it happens to
match now, and a driver that inherits a value has still not made a decision.

Three things follow from that MODE2 note. Session 4's driver work inherits all
of them; none may be assumed.

**0. SUPERSEDED — the open-drain plan, and why it is recorded rather than
deleted.** Until 2026-08-11 the plan was to clear MODE2 OUTDRV and run the
outputs **open-drain**, with a 10 kΩ pull-up to a 10 V rail sitting directly on
the LEDn pin.

That design is withdrawn. **LEDn absolute maximum is 5.5 V and the outputs are
5.5 V-only tolerant**, so a 10 V pull-up was out of spec — David's error,
confirmed against the datasheet after Claude flagged uncertainty rather than
reasoning from memory about the rating.

Kept in this file on purpose. Item 1 below exists *because* of the withdrawn
design, and a reader who does not know it was wrong will eventually reinvent it.

**0a. The output stage as it now stands (ruled 2026-08-11, recorded as given):**

| | |
|---|---|
| Per-channel stage | external N-FET, 2N7000-class |
| Gate | 1 kΩ resistor from the LEDn pin |
| Drain | the dim line and its 10 V pull-up |
| Source | common ground |
| PCA9685 output mode | **totem-pole** — MODE2 OUTDRV **set**, driving the gate |
| DMM probe point | **the FET drain. Never the LEDn pin.** |

**1. The stage inverts, and that is a safety inversion until measured.**
The expectation is that the FET stage inverts what the dim line sees relative to
the LEDn pin, so `INVRT` (MODE2 bit 4) compensates.

The consequence is not cosmetic. Our contract declares `PwmLevel(duty=0.0)` as
the safe state, meaning dark. If the polarity is wrong end-to-end, **duty 0.0
drives the channel to full brightness** — the declared safe state becomes the
most dangerous one, and every fail-safe drill would pass in software while
lighting the tank at 100%.

Rule: the driver must **prove on the bench that duty 0.0 measures dark** before
it is trusted, and that check belongs in the bring-up procedure, not in a
comment. `INVRT` is the knob that corrects it. Verify with a meter at the FET
drain, not by reasoning about the datasheet.

**2. PWM frequency is pinned at 500 Hz, from bench findings.**

| | |
|---|---|
| XLG-AB dimming window (Mean Well spec) | 100 Hz – 3 kHz |
| Documented quirk | above 2 kHz, spurious triggering at 10–15% duty |
| **Usable window we treat as valid** | **100 Hz – 2 kHz** |
| **Pinned frequency** | **500 Hz → `PRE_SCALE = 11`, ≈508 Hz actual** |

`25 MHz / (4096 × (11+1)) ≈ 508.6 Hz` on the internal oscillator.

The chip default `PRE_SCALE = 0x1e` (30) is ≈196.9 Hz. That is *inside* the
window but only about 2× above its floor — low margin, and it was never a chosen
value, just what the chip powers up with. 500 Hz sits comfortably mid-window,
clear of both the flicker floor and the 2 kHz spurious-triggering region.

**Frequency lives in device config, never as a chip default.** A driver that
inherits whatever the silicon powered up with has not made a decision.

**3. The XLG output is undefined between 0% and 8% duty.**
This is a hardware property of the driver, not something software can smooth
over. A command for 3% produces undefined behaviour — which for an LED driver
can mean flicker, nothing, or full output.

It matters more than it first looks, because **a diurnal ramp crosses that band
twice every single day.** Dawn and dusk are exactly where a lighting schedule
spends time at low duty, so this is not an edge case; it is the daily path.

**Ruled: anything under 8% snaps to 0.** Not clamped up. `duty == 0.0` is hard
off and is the declared safe state, so it must never land inside the band; of
the two options, snapping down is the one that cannot leave a channel emitting
at a duty the hardware refuses to define.

**4.** Items 0a–3 are session-4 driver requirements. Each electrical fact above
is David's, recorded as given — see the bench boundary. None may be assumed by
measurement-free reasoning.

**5. PRE_SCALE is only writable while SLEEP is set.**
The PCA9685 latches the prescaler from the sleeping oscillator. Changing PWM
frequency means: set SLEEP, write PRE_SCALE, clear SLEEP, then wait ≥500 µs for
the oscillator before setting the RESTART bit. A driver that writes PRE_SCALE on
a running chip silently does nothing, which presents as "the frequency setting
is ignored".

## Bench plan (staged; no stage proceeds without David's go)

**Stage 0 — readiness audit.** Report only. Complete 2026-08-11.

**Stage 1 — raw CLI, zero Bella's Reef code.** `i2cset`/`i2cget` only, DMM on
the **FET drain**. Read MODE1/MODE2/PRE_SCALE power-on values; set totem-pole;
wake from SLEEP; drive CH0 full-off, full-on and 50% via the LED0 registers with
David reading volts at each; set `PRE_SCALE=11` under SLEEP, read back, confirm
~508 Hz at 50%. Every command shown before running. **Every measured value goes
into this section.** Blocked until the FET stage is on the bench.

The three measured voltages are what set `INVRT_ON` in the driver. They are
ground truth with no stack in the loop.

**Stage 2 — the same facts through our stack.** Register the channel via
hardware-io (authoritative, role `light`, full safety triple), command the
identical three duty points through control-engine over the spine, David
confirms the meter matches Stage 1 exactly. **Any divergence is a driver bug by
definition** — the CLI numbers are the truth.

**Stages 4–6** — real light, fail-safe drills, CH1 — follow on David's go.

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

## Spine — cutover 2026-08-12, folded into the one boot unit 2026-08-13

The spine moved from ad-hoc `pg-dev`/`nats-dev`/`vm-dev` containers to a
compose-managed spine, first as its own `bellasreef-spine.service`, then
folded on 2026-08-13 into `bellasreef.service` covering all five services
(see Deployment discipline above — the host-systemd-app-units era in between
is a closed, dated detour). Containers: `bellasreef-nats-1`,
`bellasreef-postgres-1`, `bellasreef-victoria-metrics-1`. Named volumes:
`bellasreef_{nats,postgres,vm}-data`. All spine ports are loopback-only
(`127.0.0.1:4222/8222/5432/8428`) — the old `-dev` containers were
LAN-exposed (`0.0.0.0`) since 2026-08-09; see the RESOLVED entry above. The
old containers are stopped, not yet removed — removal is gated on David.

## Installed tooling

`git` 2.47.3 · `docker` 29.7.2 · `uv` 0.12.3 · `libgpiod` 2.2.1 · `i2c-tools` 4.4 ·
`iw` 6.9 · `rsync` 3.4.1 · `chrony` 4.6.1 · `pinctrl`

**PATH trap:** `i2cdetect`, `iw`, and `hwclock` live in `/usr/sbin`, which is not
on `PATH` for non-interactive SSH. `ssh <pi> i2cdetect` reports "not found" even
though the tool is installed — prepend `/usr/sbin:/sbin` or use absolute paths.
`uv` is no longer load-bearing for running services — the app services run as
images, not a host venv, and `bellasreef.service` invokes `docker compose`
directly — but it stays installed for local checkout/scripting use on the host.

**`pkill -f` self-match trap:** over SSH, `pkill -f <pattern>` matches the remote
shell's own command line, so `ssh pi 'pkill -f bellasreef_hardware_io; ...'` kills
the connection and returns 255 before the rest of the line runs. Bracketing the
first character (`[b]ellasreef`) only helps if the literal string appears nowhere
else in the same command — a restart line contains it twice, in the kill and in
the start. Stop and start in **separate** `ssh` invocations. (This trap predates
containers-only and applies to any process-name-matching command over SSH, not
just the deleted host units.)

**Wire, not gauge:** `BELLASREEF_NATS_URL` is the environment variable that
bites — hardware-io without it reads the probe, serves metrics and logs a
clean startup while publishing nothing at all. Metrics are not the telemetry
path — check the wire, not the gauge. (Formerly documented against the
deleted `scripts/dev/run-*.sh` dev launchers; the lesson outlives the
scripts — it applies equally to `deploy/compose.yaml`'s `environment:`
block for hardware-io.)

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

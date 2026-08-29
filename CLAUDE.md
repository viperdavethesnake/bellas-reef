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
  The one sanctioned exception is `scripts/factory-reset-pi.sh` (spec
  2026-08-15): a deliberate, typed-confirmation wipe of the three data
  volumes with a mandatory pre-reset backup.
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

Superseded 2026-08-17: the oscillator on this chip measured 26.77 MHz, so
`PRE_SCALE = 11` gave 545 Hz, not 508; the driver now computes `PRE_SCALE = 12`
(≈503 Hz) from the measured value — see "Stage 1 (PCA9685)" item 1 below. The
500 Hz pin itself is unchanged.

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

- RESOLVED (output stage order, 2026-08-15): **RP1 native PWM first, the
  PCA9685→FET chain second.** David's ruling at the bench. This is an ordering,
  not an exclusion — items 0a–5 stay live and unamended for the PCA9685 path,
  which gets its own Stage 1 when the board is back on the bus. The paragraph
  above (`i2cset`/`i2cget`, DMM on the FET drain, `INVRT_ON`) describes the
  PCA9685 leg specifically and is still pending; the RP1 leg ran 2026-08-15 and
  is recorded below.
- The PCA9685 is **not on I²C bus 1 as of 2026-08-15** — a full `i2cdetect -y 1`
  is empty, no `0x40`, no `0x70`, where 2026-08-09 saw both. The board is off
  the bench for now, by David. Re-verify presence before the PCA9685 Stage 1.
- The PCA9685 is also **not discoverable in code**: `capabilities.py` announces
  only `discover_pwm()` (RP1) and `discover_w1()`, despite its module docstring
  promising "a PCA9685 if one answers". The driver and `factory.py`'s
  `driver_type == "pca9685"` branch are complete, but nothing announces the
  capability, so no channel is adoptable. A `discover_pca9685()` is a
  prerequisite for the PCA9685 Stage 2, not for its Stage 1.
  RESOLVED 2026-08-17 (`discover_pca9685()`, PR #36, deployed as ca8ef2da):
  observed on the wire — hardware-io logged `capability announced
  hardware_source=pca9685 channels=16` at 15:51:30Z and the API's registry
  holds sixteen `pca9685` rows (channels 0–15, detail address 0x40 / bus 1).
  Announced empty when the bus is present and nothing answers. Presence check
  is one MODE1 read; `0x70` is never addressed. Note `smbus2` had never been a
  dependency of hardware-io until this PR — adoption of a PCA9685 channel
  through the stack was never runnable on the hub before it.

**Stage 1 (RP1 native PWM), CH0 and CH2 — PASSED on hardware 2026-08-15.**

Raw `/sys/class/pwm` writes only, no Bella's Reef code in the loop. DMM ground
referenced, probed at the channel's own header pin. `pwmchip0` resolved to
`1f00098000.pwm` (PWM0, ours) on this boot. Both channels held at
`period=2000000` (500 Hz, the pinned frequency) and `polarity=normal`
throughout. Every value below is David's meter reading, recorded as given.

| Commanded duty | `duty_cycle` (ns) | CH0 — pin 32 (GPIO12) | CH2 — pin 12 (GPIO18) |
|---|---|---|---|
| 0 % | 0 | **0 V** | **0 V** (≤3 mV, called meter error) |
| 8 % | 160 000 | **265 mV** | **265 mV** |
| 50 % | 1 000 000 | **~1.654 V** | **~1.654 V** |
| 100 % | 2 000 000 | **~3.309 V** | **~3.309 V** |

**The two channels agree on every point.** CH2 was probed on a different header
pin, at a different alt (`a3` vs CH0's `a0`), and produced identical readings —
so the numbers characterise the RP1 PWM0 block, not one lucky channel, and CH1
and CH3 can be expected to match without re-measuring each. Expected, not
assumed: measure before trusting a channel that drives a real load.

Two findings, both load-bearing:

1. **`polarity=normal` is correct; duty 0 measures 0 V.** The safety inversion
   that item 1 warns about does **not** apply to the RP1 path — the declared
   safe state `PwmLevel(duty=0.0)` is genuinely off at the pin, proven by meter
   rather than by reasoning. This says nothing about the PCA9685→FET chain,
   whose stage is expected to invert and whose `INVRT_ON` is still unproven.
2. **The points are linear to within a millivolt** (8 % of 3.309 V is 265 mV;
   the 50 % midpoint is 1.6545 V). The channels are consistent across the whole
   commandable range, which is what makes Stage 2 a real test: any nonlinearity
   that appears through the stack is our code, not the silicon.

Both channels returned to `duty_cycle=0, enable=0` afterwards. CH1
(`pi-pwm-1`, owned and enabled by hardware-io) was untouched for the whole run
— verified before and after, and it is the reason CH1 cannot be Stage-1'd by
raw CLI without contending with the running service. CH3 remains exported at a
leftover `period=1000000` from the 2026-08-13 bring-up; that 1 kHz is nobody's
decision and must be set to 2 000 000 before it is used. CH0 and CH2 are now at
2 000 000.

Superseded 2026-08-25 (Stage 6 register pass, run at David's direction with
nothing connected): CH1 and CH3 both driven and read back through the full
duty walk at the pinned 2 000 000 ns period, then parked at duty 0 / disabled
— the CH3 leftover-period trap no longer exists, and all four pins were read
muxed live (`pinctrl get 12,13,18,19`). hardware-io no longer held CH1 (only
adopted channels are owned; `pi-pwm-1` is not adopted post-reset). Register
and mux level only — a channel still meets a meter the day it first drives a
real load. See `docs/drafts/2026-08-25-stage6-register-pass.md`.

**Stage 1 (PCA9685), CH0 — PASSED on hardware 2026-08-15.**

Board rewired onto I2C bus 1 the same afternoon and answering at `0x40` plus
`0x70` (ALLCALL) again. Raw `i2cset`/`i2cget` only, no Bella's Reef code in the
loop. Power-on registers read identical to the 2026-08-09 baseline: MODE1
`0x11`, MODE2 `0x04`, PRE_SCALE `0x1e`, ALLCALLADR `0xe0`.

Configured as the driver does it: all channels parked full-off via ALL_LED,
`PRE_SCALE = 0x0b` written while SLEEP was set, MODE2 `0x04` (totem-pole,
INVRT clear), then MODE1 `0x21` to wake and `0xa1` to RESTART.

Measured at 544.8 Hz, full scale 3.307 V. Every value is David's meter reading.

| Commanded | LED0 registers | Off-count | Measured | As % of 3.307 |
|---|---|---|---|---|
| 0 % | `00 00 00 10` | full-off bit | **0 V** | 0 % |
| 8.008 % | `00 00 48 01` | 328 | **265 mV** | 8.01 % |
| 25 % | `00 00 00 04` | 1024 | **828 mV** | 25.04 % |
| 50 % | `00 00 00 08` | 2048 | **1.654 V** | 50.02 % |
| 75 % | `00 00 00 0c` | 3072 | **2.481 V** | 75.02 % |
| 100 % | `00 10 00 00` | full-on bit | **3.307 V** | 100 % |

Worst error across the range is 0.04 percentage points. The counted-duty
encoding is correct and the full-on/full-off bits behave as distinct from
counted values. At both extremes the meter reads 0 Hz, which is right: a static
level has no edges.

**1. `INVRT_ON = True` in the driver is WRONG for the stage measured here.**
Duty 0 measured 0 V with MODE2 INVRT **clear**. Setting INVRT inverts that,
so the driver as written would drive this channel to 3.307 V when commanded to
its declared safe state. This is item 1's failure mode reached from the
opposite direction: the code assumed inversion, the bench found none. Not
changed yet, because whether this is the final output stage is David's ruling.
Whichever stage ships, the constant must match a measurement rather than an
expectation.

- RESOLVED (2026-08-17, David's ruling: these CLI measurements are final and
  the driver's constants must match them). `INVRT_ON` is now `False` in
  `services/hardware_io/bellasreef_hardware_io/drivers/pca9685.py`, and the
  prescaler is computed from the measured oscillator rather than the
  datasheet's: `PCA9685_OSC_HZ = 26_770_000`, `PCA9685_PRE_SCALE =
  round(PCA9685_OSC_HZ / (4096 * 500)) - 1` = **12** (≈502.7 Hz actual, versus
  11's measured ≈545 Hz). Tests assert the measured values and record why 11
  was wrong. History above is kept. Merged as PR #39, deployed as 4fa2ba8. A FET stage
  inserted later gets measured, not reasoned about — the constant flips on a
  meter reading and nothing else.

**2. The internal oscillator runs ~7.1 % fast, and the error is a constant
ratio.** Two prescaler values, both measured:

| PRE_SCALE | Computed @ 25 MHz | Measured | Implied oscillator |
|---|---|---|---|
| 11 | 508.6 Hz | **544.7 / 544.8 Hz** | 26 773 094 |
| 4 | 1220.7 Hz | **1307 Hz** | 26 767 360 |

So `osc_hz ≈ 26.77 MHz` for this chip, stable across a 2.4× frequency span.
One calibration number per chip is enough. This is why frequency-as-config
needs a measured oscillator field and not a hardcoded 25 MHz: reef-pi's driver
takes a configurable frequency and divides it by a constant `clockFreq =
25000000`, so asking it for 500 Hz on a chip like this one silently returns
545. See `docs/superpowers/specs/2026-08-15-driver-hardware-config.md`.

**3. The ALL_LED registers (`0xFA`–`0xFD`) do not read back what is written.**
They return `0x00` regardless. The write lands: after writing `00 00 00 10`
every per-channel register read `00 00 00 10`. Verify an ALL_LED write through
a per-channel register, never by reading it back, or a successful write looks
like a failed one. The driver writes ALL_LED at open and deliberately does not
read it back; adding a readback assert there would break startup in a way that
presents as hardware failure.

**4. Bench instrument note.** David's DMM duty function reads the **complement**
of the commanded duty (50 % → 48, 25 % → 68, 75 % → 21.5) and saturates to 100
below roughly 10 % duty. It is readable as `100 minus displayed`. The voltage
ratio is the precise measurement and the duty readout is a sanity check.
Recorded because it looks exactly like an inverted output on first sight, and
is not: at 25 % commanded the voltage read 828 mV, which is 25 %, not 75 %.

**5. CH1 agrees with CH0.** Three points measured on CH1 (registers `0x0A`–
`0x0D`) after CH0's six, abbreviated because CH0 had already established the
encoding:

| Commanded | CH0 | CH1 |
|---|---|---|
| 0 % | 0 V, 0 Hz | **0.9 mV, 0 Hz** |
| 50 % | 1.654 V, 544.9 Hz | **1.654 V, 544.8 Hz** |
| 100 % | 3.307 V, 0 Hz | **3.308 V, 0 Hz** |

As with the RP1 block, two channels agreeing characterises the chip rather
than one lucky output. Same caveat: expected, not assumed, for the remaining
fourteen.

Both channels returned to `00 00 00 10` (duty 0) afterwards. Chip left awake at
MODE1 `0x21`, MODE2 `0x04`, PRE_SCALE `0x0b`.

**Cross-silicon agreement.** The PCA9685 and the RP1 PWM0 block produce the
same voltages at the same commanded duty, within 2 mV at every comparable
point (0 V, 265 mV, 1.654 V, ~3.308 V). Four channels across two chips. That
is a strong check on the measurement method and it means either silicon can
drive a channel with no change visible above hardware-io, which is what the
driver interface promised and had not previously been tested.

**Stage 2 — the same facts through our stack.** Register the channel via
hardware-io (authoritative, role `light`, full safety triple), command the
identical duty points through control-engine over the spine, David confirms the
meter matches Stage 1 exactly. **Any divergence is a driver bug by definition**
— the CLI numbers are the truth.

Run it on **the same channel and the same probe point** as that path's Stage 1,
so the stack is the only variable. For RP1 that means CH0 / pin 32 against the
four rows above — not CH1, which would introduce a second variable. The 8 % row
is the one that exercises `snap_duty`; commands below 8 % must measure 0 V, not
265 mV, because the driver snaps them down before they reach the pin.

**Stage 2 — PASSED on hardware 2026-08-17, both legs.** Holds set from the
iOS app's Lighting tab (sim, paired client) → API → NATS → control-engine →
hardware-io → pin. Meter at the same probe points as each leg's Stage 1. Every
value below is David's meter reading, recorded as given. Engine publications
confirmed in `docker logs bellasreef-control-engine-1` for each row.

| Hold | RP1 CH0, pin 32 (`pi-pwm-0`, Light 0) | PCA9685 CH0, LED0 (`pca9685-0`, Light 1) |
|---|---|---|
| 0 % | **~0 V** | **0 V** |
| 8 % | **265 mV** | **265 mV** |
| 50 % | **1.654 V** | **1.654 V** |
| 100 % | **3.308 V** | **3.308 V** |
| 5 % | **0 V**, truth line reads 0 % | **0 V** |
| Release | 0 V, engine published duty 0.0 | 0 V |

Every row within 1 mV of that leg's Stage 1 CLI number. The 5 % row is
`snap_duty` proven end-to-end on both silicons: the engine published 0.05x,
hardware-io snapped it to 0 and reported 0 %. `INVRT_ON = False` is confirmed
through the stack — the declared safe state is dark, commanded, not idle.

Two things learned on the way, both load-bearing:

1. **The PCA9685 driver never initialised the chip.** Voltages matched because
   the counted-duty encoding is prescale-independent — but the frequency was
   still Stage 1's leftover (PRE_SCALE `0x0b`, ≈545 Hz), and `i2cget` showed
   MODE1/MODE2/PRE_SCALE untouched since 08-15. `Pca9685Device.initialise()`
   was written, tested, and had **no production caller**: `app.py` brings
   actuators up by duck-typing `driver.open()`, the RP1 channel has one, the
   PCA channel did not. On a cold chip (MODE1 `0x11`, SLEEP set, oscillator
   off) this would have meant every command landing on a chip generating no
   PWM at all — dark, and silently so. Fixed the same day (`Pca9685Channel.
   open()` → `Pca9685Device.ensure_initialised()`, once per chip). The
   fix merged as PR #40, deployed as `974faff` (hardware-io logged
   `pca9685 initialised address=0x40 pre_scale=12 invrt=false`), and David
   re-read the 50 % row on LED0: **502.9 Hz**, 1.654 V — against 502.7
   predicted from the measured 26.77 MHz oscillator and 544.8 measured on
   Stage 1's leftover prescaler. Follow-ups: `open()` is a required member
   of the `ActuatorDriver` Protocol since 2026-08-18 — with the Protocol
   tightened and nothing else changed, `mypy --strict` flagged 33 sites,
   every one a test fake and neither real driver, which is the gap that
   would have caught the PCA9685 at gate time; `app.py` calls `open()`
   unconditionally now instead of duck-typing it. Chip state on the wire
   (read-back PRE_SCALE / frequency / INVRT / initialised, per chip) — ruled
   2026-08-18 by David: **option A**, a per-chip Hardware surface on the
   System tab, not a key in the capability `detail` (which is identity, per
   #38) and not a field on the adopted device row. It is the backend half of
   the UX review's Tier C (C1 Hardware leaf, C2 relocating register state —
   `docs/bellas-reef-ios-ux-review.md`) and is designed there, not shipped
   standalone. Needs a new message type (contracts MINOR), API store +
   endpoint, iOS.
   RESOLVED 2026-08-22/23 (shipped): backend #61/#62 (migration 0020,
   `/api/v1/hardware`), iOS #9/#15 (System tab Hardware leaf), contracts
   4.2.0. The C1/C2 rows in the 2026-08-23 UX review are marked Shipped.
2. **Adopting a channel restarts hardware-io** (`assignment_restart`: "exiting
   to rebuild from registry"), and on the way down it logged `failed to
   publish actuator state … reason=safe_state` for both pi-pwm channels — the
   safe-state publish racing the NATS close. Not investigated yet.
   RESOLVED 2026-08-18: `HardwareIO.shutdown()` now runs `supervisor.stop()`
   (safe drive + trip-state publish) *before* `spine.close()` — the order was
   reversed until then, so every shutdown publish failed into a closed spine.
   The NATS-*outage* variant of the same swallowed publish is separately
   handled by the reconnect republish (#68, `_republish_safe_states`).
   Verified 2026-08-28 on the hub: zero `failed to publish actuator state`
   lines in the current container's logs, which include the 2026-08-25
   rebuild's `assignment_restart`.

Bench notes: the engine slewed at 1 %/s in 1 % steps at the time, so 100 → 5 %
took ~95 s. Resolved since: holds carry a per-hold `snap` | `ramp` transition
(#42, contracts 3.8.0), the global slew is 0.05/s (#43, 0 → 100 % in ~20 s),
and #43 also fixed the arrival step of a slew being swallowed by the deadband
— David measured Ramp 100 % on LED0 land at **3.294 V / "Now 99 %"** on
2026-08-17 (fail), and on 2026-08-18 after #43 both **Ramp and Snap 100 %
passed** from the app, hold and release. David's meter was on CH1 from the
end of Stage 1 when the PCA leg began; a CLI `i2cset` full-on to LED0 read 0 V
until the probe was moved, then the register was written back to full-off and
the leg re-run from the app. The stack's "Now" reads the last commanded duty
and cannot see a CLI write — by design (`read_back()` is None).

**Schedule acceptance — PASSED on hardware 2026-08-23, 11:18 PDT.** The
lighting-schedules feature (#60 backend, iOS #14) proven end to end on the
PCA9685 leg: schedule "Not Interesting" (zone America/Los_Angeles, anchor
clock) created and assigned from the iOS app to `pca9685-0` ("Meter Check",
adopted fresh after a full device wipe that morning). Engine picked the
assignment up within a tick, slewed at 0.05/s (`lighting:converge` 45→79 %),
and arrived on the curve at `lighting:ramp` duty 0.7919. **Meter at LED0:
2.620 V, against 2.619 V predicted** (0.792 × the measured 3.307 V full
scale) — 1 mV agreement, Stage-2 method, same probe point. The spec's
acceptance named `pi-pwm-0`; run on `pca9685-0` instead (the channel David
adopted), which the cross-silicon agreement row above makes equivalent.

Overrides over the schedule, same sitting (11:19–11:20 PDT), all PASSED:

| Test | Audit row | Meter |
|---|---|---|
| Snap hold 15 % | `override.created` duty 0.15, transition snap, 15 min | **0.496 V** = 0.15 × 3.307, exact |
| Snap release | `override.released`, reason manual | **2.645 V** = curve-now 80.0 % |
| Ramp hold 15 % + release | duty 0.15 transition ramp; released manual | both legs agreed |

Three-way agreement throughout: audit log, engine publications, meter. The
released-with-reason rows also close the old E1 audit check. One reading
mid-test looked like a discrepancy (commanded "20 %", measured 0.496 V);
the audit said duty 0.15 and David confirmed 15 % was the actual input —
the stack was exact, the memory wasn't, and the audit log settled it, which
is the point of having one.

**Stages 4–6**, resolved 2026-08-25 by David except the drills:
- Stage 4 (real light) SKIPPED by ruling — "we did meter tests, the results
  are no different"; the Stage 1/2 characterization stands.
- Stage 6 (remaining channels) done the same day at register/mux level
  (`docs/drafts/2026-08-25-stage6-register-pass.md`); the electrical half is
  folded into load-hookup — a channel meets a meter the day it first drives
  a real load.
- **Stage 5 — PASSED on hardware 2026-08-25** (run record:
  `docs/drafts/2026-08-25-stage5-drills.md`). D1 engine stop: safe at 30.0 s,
  meter 1.984 V → 0 V, recovery by slew from dark. D2 hardware-io host
  `kill -9`: ~4 s held-at-last-duty exposure, safe-state-first restart, dip
  to dark caught on the meter. D3 NATS 44 s outage: safe at ~30 s, ~9 s
  self-heal — register-verified only; **the D3 meter word was never taken**
  (the sitting ended first), recorded as the weaker basis it is. The doc's
  pre-run "≤ 15 s" expectations were a conflation — see the RESOLVED finding
  in that file. With this, the bench plan is CLOSED except load-hookup day —
  a channel still meets a meter the day it first drives a real load.

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

**`docker kill` suppresses the restart policy** (measured 2026-08-14, docker
29.7.2). `docker kill --signal=<any> <c>` marks the container manually-stopped,
so `restart: unless-stopped` declines to restart it even when the process exits
on its own afterwards. Measured both ways on hardware-io: killed via the daemon
API the guard fired, the process exited 70, and the container **stayed dead**
(`RestartCount=0`); signalled from inside with `docker exec <c> python -c
"import os,signal; os.kill(1, signal.SIGUSR1)"` the identical exit restarted
normally (new PID, `RestartCount` +1, ~15 s).

This is an artefact of the kill API, **not** of the recovery path — a genuine
stall exits the process with nobody calling `docker kill`. It matters because
testing recovery the obvious way reports a failure that production would not
have, and could be misread as "the restart policy is broken." Signal PID 1 from
inside. `scripts/drill-restart.sh` does, and `docs/host-setup.md` §7 records
both rows.

Related: `.State.ExitCode` reads `0` on a *running* container even when its
previous life ended at 70. After a restart-policy recovery the exit code lives
in `docker events --filter event=die`, not in `inspect`.

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

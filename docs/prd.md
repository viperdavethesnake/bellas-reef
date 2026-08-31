# Bella's Reef — Product Requirements Document

**Version:** 2.0
**Owner:** David / Bella's Reef LLC
**Date:** 2026-08-12
**Status:** Active — approved by David 2026-08-12, supersedes v1.4. Flagged for a further review pass.

### Changelog

- **2.0 (2026-08-12)** — Identity correction. This version records what the
  product actually is, correcting drift in both directions: things built with
  no PRD requirement behind them (device management / PWM discovery, §8 R-PWM
  block) and things the PRD asserted that the product abandoned (Pi-5-only
  platform, Docker Compose runtime, open-drain PCA9685 output). Material
  changes: platform target restated as ARM SBCs with the Pi 5 as the current
  test platform and deliberate outlier (RP1); PWM discovery/management
  codified as the core of Phase 1; deployment corrected to native systemd
  services as-deployed; R10a corrected to the ruled electrical design
  (totem-pole into an external N-FET stage) and to Pi-native PWM as the
  first source with PCA9685 as the many-channel option. No new scope: every
  requirement added here transcribes a ruling already made and, in most
  cases, code already built. Anything in this document that is neither built
  nor ruled is marked as such.
- **1.4 (2026-08-12)** — R1 rescoped to `control_authority: authoritative`
  (records the 2026-08-10 change; advisory/observe_only must not carry the
  safety triple).
- **1.3 (2026-08-09)** — G3 footnoted for the WebSocket: REST client 100%
  generated; stream transport hand-written as documented exception; frame
  decoding via types generated from published frame JSON Schema.
- **1.2 (2026-08-09)** — Auth restated: device-bound refresh + short-lived
  JWT; "no local-trust mode" → "no unauthenticated operation" with TOFU
  bootstrap per `docs/contracts/auth.md`; paired phones renamed `/clients`.
- **1.1 (2026-08-09)** — Licensing (Q3): AGPL-3.0 backend + dual commercial,
  Apache-2.0 contracts, closed-source paid iOS app.
- **1.0 (2026-08-09)** — Initial draft.

---

## 1. Problem Statement

Reef aquarium automation on open hardware is effectively dead. The incumbent
open-source option (reef-pi) is a 2022-era Go monolith with a bus factor of
one: its GPIO layer assumes deprecated sysfs interfaces broken by modern
boards, its frontend is unmaintained, and its architecture provides no
fail-safe behavior when the host crashes — the failure mode that actually
kills livestock. Commercial controllers (Neptune Apex, GHL) are closed,
cloud-dependent, and expensive.

Reefkeepers with technical skills have no modern, safe, maintained, open
platform to run on current hardware. The cost of not solving it: tanks run on
abandonware, hobbyists pay $800+ for closed ecosystems, or they hand-roll
one-off scripts with no safety engineering.

## 2. Product Vision

Bella's Reef is a production-grade, open reef automation platform published by
Bella's Reef LLC: an open (AGPL) backend running on commodity ARM single-board
computers, controlled by a paid native iOS app, with safety as a first-class
architectural concept.

**Platform identity (corrected in v2.0):** the backend targets **ARM SBCs
running modern Linux (kernel 6.x+)** — not one board. The Raspberry Pi 5 is
the *current test platform* and is deliberately the outlier: its RP1 I/O
controller is unlike anything else on the market, so a system proven against
it and against a conventional SoC board (Raspberry Pi 3B+, BananaPi M64, or
similar) is proven portable. Hardware access goes through current kernel
interfaces only (libgpiod v2 character device, `/sys/class/pwm`, kernel w1) —
never board-specific libraries or deprecated sysfs GPIO — because that is
what makes one codebase serve many boards.

**Phase 1 in one sentence:** prove the full vertical — sensing, PWM device
management, lighting control, alerting, history, fail-safety, iOS app — on
the reference tank, where **PWM discovery/management/operation is the core
deliverable** (lighting is its vehicle) and 1-wire temperature is the solved,
portable sensing baseline. Phase 2 extends the same machinery to pumps,
relays/heat, ATO, and dosing, and to distributed spokes.

**Product principles (non-negotiable):**

1. **Build once, build right.** Every component is the production choice from
   the first commit. No staging-grade stand-ins, no "swap it later."
2. **Modern floor, no legacy tax.** Kernel 6.x+, arm64, current stable
   toolchains, iOS 26+. Old platforms unsupported, stated plainly.
3. **Fail-safe is architecture, not a feature.** Every authoritative actuator
   has a declared safe state and enforceable interlocks below the control
   logic. Software bugs must not be able to kill a tank.
4. **API-first.** The API contract is the product. Every client consumes the
   same versioned, self-documenting API. Nothing reaches around it.
5. **The PRD is the ceiling.** Scope enters this document only by the owner's
   explicit decision — never from review, archaeology, or "while we're in
   there." (The predecessor projects died of accretion; see
   docs/prior-art-review.md header.)

## 3. Goals

| # | Goal | Measure |
|---|------|---------|
| G1 | Phase-1 tank (temperature monitoring + alerting, managed PWM lighting) runs unattended on one SBC hub | Reference tank, 30 consecutive days without manual intervention |
| G2 | Provable fail-safety | Kill the hub process, kill services, pull power mid-cycle: all authoritative actuators reach declared safe state within their timeout, every drill |
| G3 | Generated, drift-free iOS client | REST client 100% generated from OpenAPI; stream frames decoded via types generated from published frame schema; contract drift = compile error. Only the WS transport is hand-written (v1.3 footnote) |
| G4 | Adding hardware sources or spokes requires no contract changes | A new PWM source (or, Phase 2, an ESP32 spoke) joins by implementing the existing driver contract / publishing on the existing subjects — zero changes to engine, API, clients |
| G5 | Installable by a stranger | Fresh supported SBC to running system via documented steps in under 30 minutes |
| G6 | Portability proven, not claimed | The identical codebase runs the Phase-1 vertical on the Pi 5 (RP1) **and** one conventional-SoC ARM board, differing only in host setup documentation |

## 4. Non-Goals (v1)

- **ESP32/Pico edge nodes.** Phase 2. The contracts they will speak are
  designed now; nothing is built for them.
- **Home Assistant integration.** Clean later add off the spine.
- **Android app.** iOS first; generated-client approach keeps it a future
  decision, not a debt.
- **Cloud service / hosted accounts.** Self-hosted, local-network +
  user-managed remote access. (Push relay is Q1.)
- **32-bit OSes, kernel <6.x, SD-card primary storage, iOS <26.** Unsupported,
  stated plainly. (v2.0 note: the v1.x "Pi 4 or earlier unsupported" line is
  withdrawn — conventional-SoC boards including Pi 3B-class are validation
  targets per G6. What is unsupported is old kernels and 32-bit userlands,
  not board families.)
- **Built-in charting/BI suite.** VictoriaMetrics is Grafana-compatible;
  the apps show operational state and focused charts only.
- **Multi-user roles/RBAC.** Single operator; paired clients are fully
  trusted. Real auth from the first endpoint, no role system.
- **Vendor-cloud light control (Kessil K-Link etc.).** Advisory/vendor-bridge
  territory, Phase 2+; see docs/device-classes.md.

## 5. Users

- **Primary (v1):** technical reefkeeper, comfortable with SSH and wiring.
  Currently on reef-pi, a dead fork, or hand-rolled scripts.
- **Secondary (post-v1):** capable hobbyist following precise documentation.
  Drives G5; not the v1 UX design center.

## 6. User Stories

**Safety (highest priority)**
- Every actuator the system commands reverts to its declared safe state when
  the controller stops responding, so a crash cannot cook, flood, or blind a
  tank.
- (Phase 2) The ATO pump is limited by a hard max-runtime interlock enforced
  below control logic, so a stuck sensor cannot cause a flood.
- (Phase 2 gate) Shadow mode: the full pipeline runs and journals every
  intended actuation while actuating nothing, to validate logic on a real
  tank before trusting it.

**Device management (v2.0 — the Phase-1 core)**
- As an operator, I can see what controllable hardware my hub has — PWM
  channels by source, the 1-wire bus — without editing files over SSH.
- As an operator, I assign a channel a name, a location, and a role ("Blue
  LEDs / Display tank / light"), because no bus can discover what a human
  wired; the app then surfaces the device where its role belongs.
- As an operator, discoverable hardware (1-wire probes, each carrying its
  own ROM identity) announces itself and I adopt it; undiscoverable slots
  (PWM channels) exist only by my declaration, and a taken channel refuses a
  second declaration.
- A device keeps its identity — name, thresholds, history, alert episodes —
  when its wiring moves to different silicon. Rebinding is not re-creation.

**Core control**
- Multi-channel LED scheduling with diurnal/ramp profiles so lighting follows
  a photoperiod without manual switching; manual override with visible
  auto-revert.
- (Phase 2) Temperature control with hysteresis; equipment scheduling;
  journaled dosing with reconciliation; ATO with consumption tracking.

**Monitoring & alerting**
- Live sensor state and alert history on my phone, anywhere I can reach the
  hub.
- Threshold alerts with hysteresis, and silence alerts when a sensor stops
  reporting — "nobody knows" is its own alarm, distinct from "out of band."
- A complete append-only audit record of commands, config changes, auth
  events, and state transitions, queryable via API.

**Operations**
- Deploy and upgrade with one verified command; rollback is a revision
  change.
- One-command backup producing a restorable archive; restore onto fresh
  hardware is an hour, not a rebuild.

## 7. Architecture (decided, as-deployed)

### 7.1 Topology

Phase 1: single SBC hub. Three Python services + spine + stores, running as
**native systemd services** with per-service `EnvironmentFile` configuration
(v2.0 correction: the v1.x containerized-topology description never matched
the deployed system; supervision, watchdog restart, and the deploy gate are
built on systemd):

- **hardware-io** — sole owner of hardware interfaces (`/dev/gpiochip*`,
  I2C, `/sys/class/pwm`, kernel w1). Announces capabilities, instantiates
  drivers from registry assignments, enforces interlocks and heartbeat-loss
  safe-state locally. Knows nothing about reef logic. Must run with the
  database dead.
- **control-engine** — all control loops, scheduling, interlock supervision.
  Sole command publisher. Emits nothing while system time is untrusted.
- **api** — stateless FastAPI front door: REST + WebSocket bridge to NATS,
  registry writes, telemetry and audit writers (spine consumers with
  Postgres/VM access live here, keeping hardware-io store-free).
- **postgres 17** — devices/registry, auth, alert episodes, audit (append-only
  by trigger), hub identity, dosing journal (Phase 2). Alembic, forward-only.
- **victoria-metrics** — all telemetry and derived series, push-based writer,
  retention/downsampling, Grafana-compatible.
- **NATS + JetStream** — durable commands with expiry and idempotency;
  retained registry/capability announcements; core pub/sub telemetry fan-out.

Phase 2 = spokes (another SBC's hardware-io, or ESP32 firmware) publishing on
the same subjects over the network; deployment topology change only (G4).

### 7.2 Locked technology decisions

| Layer | Decision | Notes |
|---|---|---|
| Host | ARM SBC, arm64, Linux kernel 6.x+ | Pi 5 = current test platform (RP1 outlier); one conventional-SoC board required for G6. Per-board facts live in host docs, never in code |
| Storage | NVMe/SSD required for production hubs | SD unsupported for production (WAL + metrics ingest). Dev-box deviations recorded in Verified host facts |
| Runtime | Native systemd services, EnvironmentFile config, journald logs | v2.0 as-deployed correction (`deploy-pi.sh` deleted 2026-08-30). Now: CI green → `v*` tag → release workflow green → `update-hub.sh` on the hub → telemetry verified, all of it |
| Relational | PostgreSQL 17, Alembic forward-only | |
| Time-series | VictoriaMetrics, push-based writer | |
| Spine | NATS + JetStream | Durable commands (expiry + idempotency, dedup at terminal stores); retained announcements; telemetry fan-out |
| API | FastAPI + Pydantic v2, OpenAPI-first | Spec + frame schemas published as one CI artifact |
| iOS | Swift/SwiftUI, iOS 26+, swift-openapi-generator | Native platform conventions are requirements, not debates |
| Web UI | Deferred to Phase 5 (hardening); structural config via API/Swagger meanwhile | v2.0 records the standing deferral ruling |
| Auth | Device-bound refresh + short-lived JWT; TOFU bootstrap per docs/contracts/auth.md | No unauthenticated operation; /info and /healthz only public endpoints |
| Hardware access | libgpiod v2 char device; `/sys/class/pwm` (the kernel PWM ABI); kernel w1 | No deprecated sysfs GPIO, no board-specific libraries |
| Observability | Structured JSON logs + Prometheus metrics per service; hub telemetry in VM | |
| CI | Lint, mypy --strict, tests, multi-arch build; env-dependent skips fail the gate; integration tests use ephemeral infra with a production-endpoint guard | |

### 7.3 Versioned contracts (published artifacts)

1. **NATS subjects + Pydantic payload models** (`bellasreef.*`): sensors,
   commands, state, heartbeats, alerts, silence, registry/capability
   announcements. Semver; the versioning table in
   docs/contracts/nats-subjects.md governs bump class. The pre-1.0 exception
   closes permanently at the first tagged release.
2. **OpenAPI spec + stream frame JSON Schemas** — one artifact; source of
   truth for all clients.
3. **Hardware driver interface** — async reads with per-driver cadence,
   calibration hook, chip-label addressing; the seam that makes G4/G6 true.

## 8. Requirements

### P0 — Phase 1 does not ship without these

**Safety framework** *(built; drills verified except where noted)*
- R1. Every actuator registration declares `control_authority`.
  `authoritative` registrations must declare safe state, max continuous
  runtime, and heartbeat timeout — missing any is rejected. `advisory` /
  `observe_only` must **not** declare a safe state — one is rejected, because
  the system cannot enforce what it cannot command.
  - *AC:* heartbeat absent past timeout → hardware-io drives safe state +
    audit event; verified for process kill, service kill, NATS outage.
- R2. *(Phase 2, stays P0 for its module)* ATO hard interlock below control
  logic, latch-until-operator.
- R3. *(Phase 2 gate)* Shadow mode: journal every intended actuation,
  actuate nothing; mode transition logged and authenticated.
- R4. Command lifecycle: durable, idempotent, expiring; expired commands are
  dropped-and-audited at the consumer, never executed late. *(built,
  wire-tested)*

**Device management (v2.0 — codifies the built registry)**
- R-DM1. **Capabilities are discovered facts.** hardware-io announces what
  controllable hardware exists (PWM sources with channel counts from the
  kernel, the w1 bus) on retained subjects; the API stores and serves them
  with per-channel bound state. The hub never assumes a channel count.
- R-DM2. **Devices are operator decisions.** A device binds a capability
  channel (or a discovered probe) with a stable id plus operator-owned name,
  location, and role. Role (`light` now; `heater`/`pump`/`doser`/`outlet`
  reserved) determines where clients surface the device.
- R-DM3. **Adopt vs declare.** Hardware with intrinsic identity (1-wire ROM)
  announces and is adopted; identity-less slots (PWM channels) exist only by
  declaration, and a taken channel returns conflict, never a second identity.
- R-DM4. **Identity survives rebinding.** Matching is on binding identity
  before creation; moving a device to different silicon changes only its
  driver binding — name, thresholds, episodes, and series continue.
  Removing a device removes all its representations: row, retained
  announcements, series.
- R-DM5. **PWM sources are interchangeable drivers** behind one contract:
  native SoC PWM (`/sys/class/pwm`) and PCA9685-class I2C expanders,
  selectable per channel, invisible above hardware-io. Board pin mappings
  are host documentation, not code.
- R-DM6. Operator flow is API-first (find capabilities → assign), with a
  seeding CLI that writes through the same API. *(App find/assign screens:
  staged, not yet built.)*

**Control modules**
- R5–R6, R8–R9. Temperature control, ATO, equipment, dosing: **Phase 2**,
  gated behind relay drivers + drills + shadow mode. Wording unchanged from
  v1.4; sequencing corrected in §10.
- R7. Lighting: multi-channel PWM scheduling, diurnal/ramp profiles per
  channel, per-profile timezone, midnight-wrap interpolation, converge-with-
  slew on restart, overrides as monotonic durations with lapse-on-wake,
  clock-trust gating. *(built and tested; has never driven a real light —
  see §12 status)*

**Drivers (Phase 1)**
- R10a *(corrected v2.0)*. **Day-1 slice:** DS18B20 1-wire temperature
  (multi-probe, CRC/fault discipline, measured-latency timeout floor) and
  PWM dimming from **two sources behind one contract** — native RP1 PWM
  (GPIO12/13 on the test platform) and PCA9685 over I2C. Output stage per
  the 2026-08-11 ruling: PCA9685 runs totem-pole into an **external N-FET
  per channel** (LEDn pins are 5.5 V-max; the withdrawn open-drain/10 V
  pull-up design is recorded in Verified host facts); polarity constants are
  set by bench measurement, not derivation. Sub-8% duty snaps to 0 (XLG-AB
  undefined band). Safe state = dark, proven by measurement before any hub
  wired to lights registers the driver.
- R10b. **Phase 2 drivers:** GPIO relays (normally-open contacts so
  de-energize = off), digital inputs, ADS1115, Atlas EZO. Same contract.

**Platform**
- R11. All readings and actuator states into VictoriaMetrics with the
  authority/transport label set from first write; envelope-preserving
  downsampled history via API.
- R12. Alerting: per-device thresholds with hysteresis **and per-device
  silence detection** (6× cadence, 30 s floor) as distinct episode classes
  that coexist; episodes persisted before publish; in-app delivery now,
  webhook/email before Phase-1 ship *(webhook/email: not built)*.
- R13. Append-only audit (trigger-enforced), exactly-once at rest via
  message-id dedup, queryable via API.
- R14. One-command backup (Postgres + VM snapshot + manifest with schema
  revision, contracts version, hub identity, explicit omissions);
  restore refuses forward schemas and corrupt archives loudly; restore
  round-trip proven including auth continuity.
- R15. API completeness: every capability above via the OpenAPI-documented
  API; clients use only that API.
- R16. iOS app: live dashboard, sensor management (rename/thresholds/
  primary), alert display per the design brief's semantic-color law,
  History with honest gaps and alert bands, System/clients management.
  §7 of docs/ios-design-brief.md is review law.
- R17. Web UI: full structural-config surface — **deferred to Phase 5**;
  structural config via API/Swagger until then (standing ruling, recorded).

### P1 — fast-follow candidates (unchanged; enter only by owner decision)

Salinity-aware ATO · APNs push (Q1) · leak detection · feed mode ·
Grafana dashboard pack.

### P2 — architectural insurance (design for, do not build)

ESP32/Pico spokes (Q5) · Home Assistant · multi-tank scoping · Android ·
vendor-bridge for advisory devices (Kessil et al., per device-classes.md,
with encrypted credential storage mandatory when it exists).

## 9. Success Metrics

- Fail-safe drill pass rate 100%, run weekly (G2).
- G6 portability run: Phase-1 vertical green on a second, conventional-SoC
  board with only host-doc changes.
- Install-from-scratch < 30 min by someone who is not the author (G5).
- ≥ 99.9% on-schedule engine decision delivery, self-measured.
- 30 unattended days on the reference tank (G1).
- Post-publication: external installs; third-party drivers/spokes against the
  published contracts; **zero livestock-loss incidents attributable to
  controller logic** — the metric that matters.

## 10. Phasing (dependency-driven; no external deadline)

1. **Done:** contracts; spine + safety framework + drills; sensing vertical
   (probe → app); alerting (threshold + silence); history; auth/pairing;
   backup/restore; supervision + verified deploy; capabilities tier +
   identity-safe registry.
2. **Current:** registry-driven hardware-io (retiring file topology), seeding
   CLI, device find/assign in the app; then the **bench session** — output
   stage measured, polarity constants set from volts, first light, drills
   against the real channel. Gate unchanged: no control loop actuates a real
   tank until its drills pass.
3. **Steady-state tag (v0.1.0):** docs consolidated to as-built, stranger
   install run, status.md from verified evidence only; external review
   begins. Closes the contracts pre-1.0 exception.
4. **G6 portability run** on a conventional-SoC board.
5. **Phase 2:** relays + temperature control via shadow mode, ATO, equipment,
   dosing; spokes; vendor-bridge — each entering by owner decision.

## 11. Open Questions

| # | Question | Blocks | Owner |
|---|---|---|---|
| Q1 | Push relay: LLC-hosted APNs infrastructure vs webhook/email-only v1. Weight raised by paid-app decision | P1 push; iOS alert UX | David |
| Q2 | Remote access: documented Tailscale-first pattern vs anything built-in | Docs; iOS connectivity UX | David |
| Q3 | **RESOLVED** — licensing structure (see v1.1 changelog) | — | Closed |
| Q4 | Phase-2 hardware inventory: relay channel count, float/optical count, pH strategy (ties Q6) | R10b ordering | David |
| Q5 | Spoke firmware: custom minimal vs ESPHome-bridge | Nothing in Phase 1 | David |
| Q6 | pH probe strategy: ADS1115+analog vs Atlas EZO | R10b detail | David |
| Q7 | **(new)** Second validation board for G6: Pi 3B+ (on hand) vs BananaPi M64 vs other | G6 scheduling only | David |

## 12. Status honesty (v2.0)

This section exists so the PRD cannot silently claim more than the tree:
**verified** = sensing, alerting, history, auth, ops, registry tier per §10
item 1. **Built-unverified** = the entire lighting/PWM control path — no
photon has ever moved; PCA9685 polarity awaits the meter. **Absent** = all
Phase-2 modules, web UI, push delivery, app find/assign screens, G6 run.
The independent evidence audit (docs/review/, in progress) is the source of
record; where this section and that audit disagree, the audit wins.

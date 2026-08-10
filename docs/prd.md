# Bella's Reef — Product Requirements Document

**Version:** 1.2
**Owner:** David / Bella's Reef LLC
**Date:** 2026-08-09
**Status:** Active

**Changelog:**
- **1.2 (2026-08-09):** Auth row restated as device-bound refresh + short-lived JWT, and "no local-trust mode" restated as "no unauthenticated operation" with the TOFU bootstrap window per `docs/contracts/auth.md`. The original wording forbade the pairing bootstrap the auth design requires; the intent — nothing operates unauthenticated — is unchanged. Client-device endpoints renamed `/api/v1/clients` to stop "devices" meaning both sensors/actuators and paired phones.
- **1.1 (2026-08-09):** Q3 resolved — AGPL-3.0 backend with dual commercial licensing, Apache-2.0 contracts/OpenAPI, closed-source paid iOS app in private repo, CLA-or-no-contributions policy. Q4 partially resolved earlier — Day-1 driver slice (R10a: PCA9685 + DS18B20).
- **1.0 (2026-08-09):** Initial draft.

---

## 1. Problem Statement

Reef aquarium automation on open hardware is effectively dead. The incumbent open-source option (reef-pi) is a 2022-era Go monolith with a bus factor of one: its GPIO layer assumes deprecated sysfs interfaces broken by the Raspberry Pi 5's RP1 I/O controller, its frontend is unmaintained, its last major release predates the Pi 5 entirely, and its architecture provides no fail-safe behavior when the host crashes — the failure mode that actually kills livestock. Commercial controllers (Neptune Apex, GHL) are closed, cloud-dependent, and expensive.

Reefkeepers with technical skills have no modern, safe, maintained, open platform to run on current hardware. The cost of not solving it: tanks run on abandonware, or hobbyists pay $800+ for closed ecosystems, or they hand-roll one-off scripts with no safety engineering.

## 2. Product Vision

Bella's Reef is a production-grade, open reef automation platform published by Bella's Reef LLC. Modern stack only, no legacy accommodation, safety as a first-class architectural concept. Phase 1 runs entirely on a single Raspberry Pi 5-class board; the architecture is hub-and-spoke from day 1 so that distributed edge nodes are a deployment change, not a rewrite.

**Product principles (non-negotiable):**

1. **Build once, build right.** Every component is the production choice from the first commit. No staging-grade stand-ins anywhere in the stack, no "swap it later."
2. **Modern floor, no legacy tax.** Linux kernel 6.x+, Raspberry Pi 5+ class hardware, iOS 26+, current stable toolchains. Old platforms are unsupported, stated plainly.
3. **Fail-safe is architecture, not a feature.** Every actuator has a declared safe state and hardware-enforceable interlocks. Software bugs must not be able to kill a tank.
4. **API-first.** The API contract is the product. Every client (iOS, web, future integrations) consumes the same versioned, self-documenting API. Nothing reaches around it.

## 3. Goals

| # | Goal | Measure |
|---|------|---------|
| G1 | A complete tank (temp, ATO, lighting, equipment scheduling, dosing) runs on one Pi 5 with no external services | Full control of reference tank; 30 consecutive days without manual intervention |
| G2 | Provable fail-safety | Kill the hub process, kill the container runtime, pull power to the Pi mid-cycle: all actuators reach declared safe state within their timeout in every test |
| G3 | Generated, drift-free iOS client | Swift client is 100% generated from the OpenAPI spec; contract changes surface as compile errors, zero hand-written API bindings |
| G4 | Phase 2 requires no contract changes | An ESP32 spoke joins the running system by publishing on the existing NATS subject schema, with zero changes to control engine, API, or clients |
| G5 | Installable by a stranger | Fresh Pi 5 + NVMe to fully running system via documented steps + `docker compose up` in under 30 minutes |

## 4. Non-Goals (v1)

- **ESP32/Pico edge nodes.** Phase 2. Phase 1 designs the message contract they will use, builds nothing for them.
- **Home Assistant integration.** Valuable, not day-1 priority. The NATS/API surface makes it a clean later add.
- **Android app.** iOS first. The generated-client approach makes Android a future decision, not a debt.
- **Cloud service / hosted accounts.** v1 is self-hosted and local-network + user-managed remote access. (Push notification relay is an open question — see §11.)
- **Support for Pi 4 or earlier, 32-bit OSes, kernel <6.x, SD-card storage, iOS <26.** Explicitly unsupported. Documented as hard requirements, not recommendations.
- **Built-in charting/dashboard suite.** VictoriaMetrics is Grafana-compatible; power users point Grafana at it. The web UI and iOS app show operational state and focused charts, not a BI tool. Reef-pi spent years rebuilding a worse Grafana; we will not.
- **Multi-user roles/permissions.** Single-operator token auth in v1. Real auth from day 1, but no RBAC.

## 5. Users

- **Primary (v1):** Technical reefkeeper. Comfortable with Docker, SSH, and wiring a relay board. Currently on reef-pi, a dead fork, or hand-rolled scripts.
- **Secondary (post-v1):** Capable hobbyist who can follow precise documentation. Drives the "installable by a stranger" goal but is not the design center for v1 UX.

## 6. User Stories

**Safety (highest priority)**
- As a reefkeeper, I want every actuator to revert to its declared safe state when the controller stops responding, so that a software crash cannot cook or flood my tank.
- As a reefkeeper, I want the ATO pump limited by a hard maximum-runtime interlock enforced below the control logic, so that a stuck float/optical sensor cannot cause a salinity crash or flood.
- As a reefkeeper, I want to run the platform in shadow mode against my current controller — logging every decision, actuating nothing — so I can validate its logic on my real tank before trusting it.

**Core control**
- As a reefkeeper, I want temperature control with configurable heater/chiller hysteresis so the tank holds setpoint without equipment short-cycling.
- As a reefkeeper, I want ATO driven by sensor input with consumption tracking, so top-off is automatic and drift from baseline is visible.
- As a reefkeeper, I want multi-channel LED scheduling (diurnal/ramp profiles) so lighting follows a natural photoperiod without manual switching.
- As a reefkeeper, I want equipment scheduling (return, skimmer, wavemakers) with per-device schedules and manual override, so maintenance mode is one action, not five.
- As a reefkeeper, I want dosing executed as journaled transactions with reconciliation against container level, so a doser cannot silently drift for months.

**Monitoring & alerting**
- As a reefkeeper, I want alerts on derived signals — pH rate-of-change, heater duty-cycle creep, ATO consumption trend — so I learn about a failing heater or a leak before it becomes an emergency.
- As a reefkeeper, I want live sensor state and alert history in the iOS app so I can check the tank from anywhere I have connectivity to the hub.
- As a reefkeeper, I want a complete audit log of every command, config change, and actuator state transition, so post-incident analysis is reconstruction, not guesswork.

**Operations**
- As an operator, I want the entire system deployed and upgraded via pinned Compose images, so upgrades are atomic and rollback is a tag change.
- As an operator, I want full backup/restore of config and journals, so hardware replacement is an hour, not a rebuild.

## 7. Architecture (decided)

### 7.1 Topology

Phase 1: single Pi 5-class hub. Five services + message spine, all containerized:

```
┌────────────────────────── Pi 5 hub (Linux 6.x+, NVMe) ─────────────────────────┐
│                                                                                │
│  web-ui (SPA)   ios app (external)                                             │
│        │             │                                                         │
│        ▼             ▼                                                         │
│  ┌──────────────────────────┐        ┌──────────────┐   ┌──────────────────┐   │
│  │ api  (FastAPI, stateless)│◄──────►│ postgres 17  │   │ victoria-metrics │   │
│  └────────────┬─────────────┘        └──────▲───────┘   └────────▲─────────┘   │
│               │  NATS + JetStream           │                    │             │
│  ┌────────────▼─────────────┐               │                    │             │
│  │ control-engine           │───────────────┘ (journal/audit)    │ (metrics)   │
│  └────────────┬─────────────┘                                    │             │
│               │  same subject contract future spokes speak       │             │
│  ┌────────────▼─────────────┐────────────────────────────────────┘             │
│  │ hardware-io  (sole owner │                                                  │
│  │ of /dev/gpiochip*, i2c,  │                                                  │
│  │ 1-wire, serial)          │                                                  │
│  └──────────────────────────┘                                                  │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **hardware-io** is the only container with device access. It exposes sensors/actuators over the versioned NATS subject schema and knows nothing about reef logic.
- **control-engine** holds all control loops, interlock supervision, and scheduling. It is the sole command publisher to actuators. It does not run inside the API process.
- **api** (FastAPI + Pydantic v2) is a stateless front door: REST + WebSocket bridge to NATS subjects. Publishes commands, never touches hardware or control state directly.
- **Phase 2** = ESP32/Pico spokes publishing on the same subjects over the network. Deployment topology change only (Goal G4).

### 7.2 Locked technology decisions

| Layer | Decision | Notes |
|---|---|---|
| Host | Raspberry Pi 5+ class, arm64, Linux kernel 6.x+ | Hard floor |
| Storage | NVMe **required** | SD unsupported; Postgres WAL + metrics ingest will destroy SD media |
| Runtime | Docker Compose, pinned multi-arch (arm64/amd64) images | Least-privilege: specific devices passed to hardware-io only; no privileged containers; no k8s |
| Relational | PostgreSQL 17 | Config, dosing journal, calibration records, audit log. Alembic migrations from schema v1 |
| Time-series | VictoriaMetrics | Telemetry + derived metrics; PromQL; built-in retention/downsampling; Grafana-compatible |
| Message spine | NATS + JetStream | JetStream for durable command delivery (commands survive restarts or explicitly expire — never silently vanish); core pub/sub for live telemetry fan-out |
| API | FastAPI + Pydantic v2, OpenAPI-first | OpenAPI spec is a published, versioned artifact |
| iOS client | Swift/SwiftUI, iOS 26+, client generated via swift-openapi-generator | Zero hand-written bindings |
| Web UI | Standalone SPA container consuming the same API + WebSocket as iOS | Two independent clients keep the contract honest |
| Auth | Token auth (device-bound refresh + short-lived JWT) from the first endpoint | No unauthenticated operation; TOFU bootstrap window per `docs/contracts/auth.md`. No retrofit |
| GPIO/I2C | libgpiod v2 / kernel character device | No sysfs, no RPi.GPIO shims |
| Observability | Structured JSON logs + metrics from every service, day 1 | |
| CI | Multi-arch builds + API contract tests from repo creation | |

### 7.3 Versioned contracts (published artifacts from day 1)

1. **NATS subject schema + payload models** — e.g. `bellasreef.sensor.temp.<probe_id>`, `bellasreef.cmd.outlet.<device_id>`, `bellasreef.state.<device_id>`. This contract is the phase-2 enabler and the third-party integration surface. Semantic versioning; breaking changes are migrations.
2. **OpenAPI spec** — source of truth for all clients.
3. **Hardware driver interface** — the abstraction hardware-io implementations satisfy. Documented and stable even with one implementation in v1; this is the piece that cannot be retrofitted once drivers accumulate (reef-pi's driver-zoo failure in origin form).

## 8. Requirements

### P0 — v1 does not ship without these

**Safety framework**
- R1. Every actuator registration declares: safe state, maximum continuous runtime, and heartbeat timeout. Registration without all three is rejected.
  - *AC:* Given a registered heater outlet, when control-engine heartbeat is absent for the declared timeout, then hardware-io drives the outlet to safe state and emits an audit event. Verified for process kill, container kill, and NATS outage.
- R2. ATO hard interlock: maximum pump runtime per window enforced in hardware-io (below control logic), independent of sensor state.
  - *AC:* Given a stuck-on ATO sensor reading, when cumulative pump runtime hits the cap, then the pump is forced off, latched, and an alert fires; latch clears only by explicit operator action.
- R3. Shadow mode: full pipeline runs, every intended actuation is journaled, zero actuation occurs.
  - *AC:* Given shadow mode enabled, when any control loop decides an action, then the decision is journaled with full context and no device state changes. Mode transition is a logged, authenticated operation.
- R4. Command lifecycle: every actuator command is durable (JetStream), idempotent, and carries an expiry. Expired commands are dropped and audited, never executed late.

**Control modules**
- R5. Temperature: heater + optional chiller, hysteresis control, probe-loss handling (probe loss → safe state + alert, never last-known-value control).
- R6. ATO: sensor-driven top-off with consumption metering and baseline-drift tracking.
- R7. Lighting: multi-channel PWM scheduling with diurnal/ramp profiles per channel.
- R8. Equipment: named outlets, cron-style schedules, manual override with automatic reversion timer, one-action maintenance mode (grouped overrides).
- R9. Dosing: journaled transactions in Postgres (intent → execution → confirmation), per-dose and per-day volume caps, reconciliation workflow against measured/entered container level, drift alerting.

**Sensors/drivers (v1 hardware-io)**
- R10a. **Day-1 driver slice:** PCA9685 PWM over I2C (LED dimming; open-drain mode, parallel Mean Well XLG-AB-class drivers) and DS18B20 1-wire temperature. These two prove the full vertical: driver contract → hardware-io → NATS subjects → control-engine → telemetry → API → clients. Both implemented against the driver interface contract (§7.3.3).
- R10b. **Remaining v1 drivers:** GPIO out (relays), GPIO digital in (float/optical), ADS1115 ADC path (pH/analog), Atlas Scientific EZO (I2C). Same contract; landed after the Day-1 vertical is proven. Calibration records for all drivers stored in Postgres. Heater and ATO circuits wire on normally-open relay contacts so coil de-energize = load off — power loss itself is the ultimate safe state.

**Platform**
- R11. Telemetry: all sensor readings and actuator states into VictoriaMetrics with per-metric retention policy.
- R12. Alerting: rule engine over raw and derived signals (rate-of-change, duty-cycle, consumption trend) with at minimum webhook + email delivery in v1.
- R13. Audit log: append-only record of every command, config change, auth event, and state transition, queryable via API.
- R14. Backup/restore: one command produces a restorable archive of Postgres + config; documented restore path onto fresh hardware.
- R15. API completeness: every capability above is exposed via the OpenAPI-documented API; web UI and iOS use only that API.
- R16. iOS app (iOS 26+, SwiftUI): live dashboard, alert list + acknowledgement, manual overrides, shadow-mode review. Generated client only.
- R17. Web UI: full configuration surface (device registration, calibration, schedules, dosing setup, alert rules) + operational dashboard.

### P1 — fast-follow candidates

- Salinity-aware ATO: conductivity probe closes the loop; dual-reservoir (RODI/saltwater) top-off corrects salinity drift in both directions.
- APNs push notifications (dependent on cloud-relay decision, §11).
- Leak detection (moisture sensors + ATO-trend correlation).
- Feed mode / additional macro-style grouped actions beyond maintenance mode.
- Grafana dashboard pack shipped as importable JSON.

### P2 — architectural insurance (design for, do not build)

- ESP32/Pico W spoke firmware speaking the subject contract (build-vs-adopt-ESPHOME decision deferred, §11).
- Home Assistant integration (MQTT bridge or native integration off the NATS spine).
- Multi-tank support under one hub (subject schema already namespaces per device; keep tank-scoping in mind in config schema).
- Android client (generated from the same OpenAPI spec).

## 9. Success Metrics

**Leading (first 60 days of reference-tank operation)**
- Fail-safe drill pass rate: 100% across all drill types (G2), run weekly.
- Shadow-mode disagreement rate vs. incumbent controller: <2% of decisions after tuning, with every disagreement explained.
- Uptime of control-engine decisions delivered on schedule: ≥99.9% (measured from its own metrics).
- Install-from-scratch time (G5): <30 min, tested by someone who is not the author.

**Lagging (post-publication)**
- External installs reporting successful 30-day runs.
- Third-party driver or spoke implementations against the published contracts (the real signal the contract-first bet paid off).
- Zero livestock-loss incidents attributable to controller logic. This is the metric that matters.

## 10. Timeline & Phasing

No hard external deadline. Sequencing is dependency-driven:

1. **Contracts first:** NATS subject schema, driver interface, OpenAPI skeleton, Postgres schema v1 + Alembic. CI + multi-arch builds live before feature code.
2. **Spine + safety + Day-1 vertical:** hardware-io with safety framework (R1–R4) and the Day-1 driver slice (R10a: PCA9685 dimming + DS18B20 temp). Fail-safe drills passing before any control logic ships. Lighting schedules (R7) and temperature monitoring land first because they exercise the entire stack end-to-end with the lowest-risk actuator class — a dimmed light failing safe is a non-event; a heater is not.
3. **Control modules:** temperature *control* (R5, heater actuation) waits for GPIO relay drivers (R10b) and passed drills; then R6, R8, R9 individually, each entering service via shadow mode on the reference tank.
4. **Clients:** API completeness (R15), web UI (R17), iOS app (R16) — iOS starts as soon as the OpenAPI spec stabilizes for the dashboard surface.
5. **Hardening + publication:** backup/restore, docs, stranger-install test, LLC publication under the licensing structure in Q3 (resolved).

Gate between 2→3 is explicit: no control loop actuates a real tank until safety drills pass.

## 11. Open Questions

| # | Question | Blocks | Owner |
|---|---|---|---|
| Q1 | **Cloud relay for push:** APNs requires a hosted relay — does Bella's Reef LLC run minimal notification infrastructure, or does v1 ship webhook/email only and defer push? Weight increased by Q3 resolution: a paid App Store app makes push table stakes. | P1 push work; iOS alert UX design | David (business + architecture) |
| Q2 | **Remote access story:** Tailscale-first documented pattern vs. anything built-in. Leaning documented-pattern; confirm. | Docs, iOS connectivity UX | David |
| Q3 | **RESOLVED (2026-08-09):** Backend AGPL-3.0 with dual commercial licensing offered by Bella's Reef LLC (bundlers/OEMs buy out of the AGPL disclosure obligations). Contracts package + OpenAPI spec: Apache-2.0, same public repo, so third-party clients stay unencumbered. iOS app: closed-source, paid App Store product, separate private repo (`clients/ios/` in the public tree becomes a README pointer). Contribution policy: CLA required or contributions not accepted — LLC must retain relicensing rights for the commercial side; policy set before the repo goes public. Commercial license text to be reviewed by an IP attorney before first sale. | — | Closed |
| Q4 | **Reference tank inventory — partially resolved:** Day-1 slice decided (PCA9685 dimming + DS18B20 temp, R10a). Remaining: relay channel count, float/optical sensor count, and pH probe strategy (ties to Q6) for R10b ordering. | R10b build order | David |
| Q5 | **Phase 2 spoke firmware:** custom minimal firmware speaking the subject contract vs. adopting ESPHome and bridging. No v1 work either way; decision influences how strictly the subject schema mirrors ESPHome concepts. | Nothing in v1 (non-blocking) | David |
| Q6 | **pH/probe strategy:** ADS1115 + analog boards vs. standardizing on Atlas EZO across the board (cost vs. calibration workflow quality). | R10 details (non-blocking, both supported by driver contract) | David |

---

*Next artifacts on request: engineering ticket breakdown from R1–R17, NATS subject schema draft, or Postgres schema v1 draft.*

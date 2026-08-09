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

# Bella's Reef

Production-grade, open reef aquarium automation. Published by Bella's Reef LLC.

Runs on a Raspberry Pi 5. Safety is architecture, not a feature: every actuator
declares a safe state, a maximum continuous runtime, and a heartbeat timeout, or
it cannot be registered at all.

See [`docs/prd.md`](docs/prd.md) for what this is and why, and
[`CLAUDE.md`](CLAUDE.md) for the locked stack and the verified host facts.

## Repository layout

| Path | What |
|---|---|
| `contracts/` | Versioned wire contracts — NATS subjects, payload models, driver interface |
| `services/` | `hardware-io`, `control-engine`, `api` |
| `db/` | PostgreSQL schema and Alembic migrations |
| `deploy/` | Compose stack, Dockerfiles, systemd units |
| `docs/` | PRD, contract specs, host setup, session log |
| `clients/` | Web SPA, and a pointer to the iOS app |

## Locked out of your own tank

Nobody should be, so there is a way back in from the hub itself. Both commands
are `bellasreef`, installed with the API package, and both talk to Postgres
directly rather than through the API, because the API is the thing you cannot
authenticate to. Full procedures in
[`docs/host-setup.md`](docs/host-setup.md#10-getting-back-in-bellasreef-pair-and-bellasreef-revoke).

```bash
cd /home/david/bellasreef
alias br='docker compose -f deploy/compose.yaml --env-file deploy/.env exec api bellasreef'

br pair --ttl 600     # open a 10-minute window; pair a replacement device
br revoke --list      # every client this hub has ever paired
br revoke <id>        # turn one off, by id or by unambiguous name
```

The CLI lives in the `api` image and inherits the running container's
environment — there is no host virtualenv and no env file to source.

Replacing a phone is both commands. A pairing window *adds* a client and never
removes one, because the trust-on-first-use window is keyed on client rows
having ever existed: clearing them to "reset" the hub would reopen open pairing
to the whole LAN. So pair the new device, then revoke the old one.

## Licensing

This repository is **dual-licensed by component**. The split is deliberate: the
platform is copyleft so improvements come back, while the contracts stay
permissive so nobody needs permission to talk to it.

| Component | Licence |
|---|---|
| Backend — `services/`, `db/`, `deploy/`, everything not listed below | **AGPL-3.0-only** ([`LICENSE`](LICENSE)) |
| Contracts package and OpenAPI spec — `contracts/` | **Apache-2.0** ([`contracts/LICENSE`](contracts/LICENSE)) |
| iOS app | Closed source, paid. Separate private repository — see [`clients/ios/README.md`](clients/ios/README.md) |

**Why AGPL for the backend.** A reef controller is a networked service. Under
plain GPL, someone could run a modified Bella's Reef as a hosted product and
never publish their changes. AGPL §13 closes that: if you offer modified
Bella's Reef to users over a network, those users get the source.

**Why Apache-2.0 for `contracts/`.** The subject schema, payload models and
OpenAPI spec are the integration surface. A third party writing a client, an
ESP32 spoke, or a Home Assistant bridge should not inherit copyleft obligations
for speaking our protocol. That is the whole point of publishing a contract.

**Commercial licensing.** Bella's Reef LLC offers commercial licences that
release bundlers and OEMs from the AGPL's disclosure obligations. Contact the
LLC. Commercial licence text is pending IP-attorney review before first sale.

Source files carry [SPDX](https://spdx.dev/) identifiers, so the applicable
licence is determinable per file rather than by inference from directory.

## Contributing

**A signed CLA is required, or contributions cannot be accepted.** This is not
bureaucracy for its own sake: the LLC must retain relicensing rights to offer
the commercial licence above, and it cannot do that for code it does not have
rights to. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Status

Pre-release. No tagged version yet. Safety framework, contracts, schema v1, the
DS18B20 driver and the NATS spine are implemented and CI-verified; control
modules and clients are not.

# Session log

Ledger of completed items. One line each: timestamp, item, commit, result.

| When (PDT) | Item | Commit | Result |
|---|---|---|---|
| 2026-08-09 14:48 | S1.1 repo bootstrap, GitHub repo created private | `ba74904` | green |
| 2026-08-09 15:05 | S1.2 NATS subject spec + Pydantic contracts + driver interface + schema v1 | `13c05b5` | red (ruff format) |
| 2026-08-09 15:08 | S1.2 format fix | `ebe754c` | green |
| 2026-08-09 15:12 | S1.3 CI skeleton: ruff, mypy --strict, pytest, buildx dry run | `ebe754c` | green |
| 2026-08-09 16:05 | S2.0 item 0 fixes: prd.md rename, claude-new deleted, NULL-cadence CHECK, image digest pins | `0f70153` | green |
| 2026-08-09 16:06 | S2.4 interlock supervisor + fake drivers + 14 fail-safe drills | `0f70153` | red (drift, bind param) |
| 2026-08-09 16:08 | S2.5 migration/model drift reconciled; audit bind-param fix | `0f2694f` | red (exception type) |
| 2026-08-09 16:10 | S2.6 append-only trigger assertion corrected to DBAPIError | `eb14b35` | green |
| 2026-08-09 16:36 | S3.3 DS18B20 driver, verified against probe 28-000000bfe244 | `e098a76` | red (ruff format) |
| 2026-08-09 16:38 | S3.3 format fix | `0a1ed81` | green |
| 2026-08-09 17:05 | S3.6 pre-push hook (core.hooksPath + install script), verified blocking | `71e2ae3` | green |
| 2026-08-09 17:05 | S3.4 service skeleton: JSON logs, /healthz, /metrics, systemd unit, Dockerfile, compose | `71e2ae3` | green |
| 2026-08-09 17:05 | S3.5 sd_notify WATCHDOG=1 + LivenessGuard; restart drill passed on Pi (10.8 s freeze→safe) | `71e2ae3` | green |
| 2026-08-09 17:26 | S4.A spine: streams/consumers, registration + heartbeat publishing, command consumption; redelivered-expired command dropped+audited | `c1ba1bf` | green |
| 2026-08-09 17:26 | S4.B audit writer into audit_log; append-only trigger + idempotency_key under concurrent load | `c1ba1bf` | green |
| 2026-08-09 17:26 | S4.C CI: NATS+JetStream via docker run (services: cannot pass --jetstream) | `c1ba1bf` | green |
| 2026-08-09 17:22 | S4.0 licensing: AGPL-3.0 root, Apache-2.0 contracts, README, CONTRIBUTING (CLA), iOS pointer, SPDX headers (8 Apache / 23 AGPL) | `6f92eb3` | green |
| 2026-08-09 17:34 | S4.1 migration 0002: audit_log.message_id unique; writer ON CONFLICT DO NOTHING; redelivery test flipped to exactly-once | `16a195a` | green |
| 2026-08-09 17:34 | Note: verification against docs is not verification against the installed package (nats.jetstream did not exist in nats-py 2.15) | — | — |
| 2026-08-09 17:58 | S5.1 shared service package extracted (logs/health/liveness) so control-engine need not depend on hardware-io | `0015a32` | green |
| 2026-08-09 17:58 | S5.2 control-engine: skeleton, sole command publisher, lighting scheduler R7, clock-trust gate, NATS integration | `0015a32` | green |
| 2026-08-09 18:02 | S5.3 auth.md reviewed against contracts and committed; 5 conflicts flagged for ruling | `0015a32` | n/a |
| 2026-08-09 18:06 | S5.4 ios-design-brief.md committed; §5 `role` contract change proposed (2.0.0, no dual-publish, exception with expiry) — awaiting approval | `0015a32` | n/a |
| 2026-08-09 18:02 | S6.1 contracts 2.0.0: role required Literal, schema_version->2, pre-release exception documented with first-tag expiry | `0015a32` | green |
| 2026-08-09 18:02 | S6.2 PRD v1.2: auth row + no-local-trust wording amended; clients rename | `0015a32` | n/a |
| 2026-08-09 18:02 | S6.3 migration 0003: paired_clients, signing_keys, pairing_requests, devices.role CHECK | `0015a32` | green |
| 2026-08-09 18:02 | S6.4 _bellasreef._tcp registered on host, verified from the Mac | `0015a32` | n/a |
| 2026-08-09 18:14 | S6.5 api service: /info, /pair TOFU + 202-poll-approve, /token, /clients + revoke, /healthz; 14 lifecycle tests | `73a454b` | green |
| 2026-08-09 18:30 | S7.0 time-and-scheduling.md reviewed and committed; 4 flags raised | `03109c0` | n/a |
| 2026-08-09 18:38 | S7.1 profile schema: anchor (solar reserved+rejected), locale, on_miss, global slew knob | `395c9b8` | green |
| 2026-08-09 18:38 | S7.2 converge-with-slew on restart, from SAFE_DUTY; mid-ramp restart tested | `395c9b8` | green |
| 2026-08-09 18:38 | S7.3 migration 0004 overrides; monotonic in-run, lapse-on-wake, one active per target | `395c9b8` | green |
| 2026-08-09 18:38 | S7.4 API audit sink wired to bellasreef.audit.auth; broker outage does not break pairing | `395c9b8` | green |
| 2026-08-09 20:55 | S8.1 override creation clock-gated; shared clock predicate in bellasreef-service; boot-then-chrony sequence tested | `270e0ed` | green |
| 2026-08-09 20:55 | S8.2 migration 0005 pairing_windows + `bellasreef pair` recovery CLI; auth.md updated to as-built | `270e0ed` | green |
| 2026-08-09 20:55 | S8.3 api: GET devices/sensors, override endpoints, WS /api/v1/stream with override context on state frames | `270e0ed` | green |
| 2026-08-09 21:44 | S9.0 openapi.json exported from the app and published as a CI artifact | `4720cc7` | green |
| 2026-08-09 21:52 | S9.1 stream frame Pydantic models + JSON Schema export; bridge emits validated frames; PRD v1.3 G3 footnote | `61dd0f2` | green |
| 2026-08-09 22:52 | S9.2 explicit operation ids; all non-200 responses declared (401/403/404/409/410/503) so clients model them | pending | green |
| 2026-08-09 22:52 | S9.3 iOS repo created private and pushed: viperdavethesnake/bellasreef-ios | `d7a1b2c` | n/a |
| 2026-08-09 23:20 | S10.1 hardware-io publishes SensorReading to `bellasreef.sensor.>` — the poll loop set its Prometheus gauge and published nothing, so the whole telemetry path was dead while every unit test and every metric looked healthy | pending | green |
| 2026-08-09 23:20 | S10.2 spine tests subscribe to the wire (ok + faulted readings). Metrics are not the telemetry path; a gauge-based assertion stays green through a total publish outage | pending | green |
| 2026-08-09 23:47 | S10.3 scripts/dev/run-{api,hwio}.sh: hardware-io started without BELLASREEF_NATS_URL logs a clean startup and silently runs spineless | pending | n/a |
| 2026-08-09 23:47 | Note: `pkill -f` over SSH matches the remote shell's own cmdline — stop and start must be separate ssh calls (recorded in CLAUDE.md) | — | — |
| 2026-08-10 00:20 | S11.1 contracts 2.1.0: `bellasreef.alert.<device_id>` + SensorAlert (MINOR — new subject on a new message type, per the versioning table; no migration) | pending | green |
| 2026-08-10 00:20 | S11.2 migration 0006: devices.alert_{min,max,clear_margin} + sensor_alerts episodes; CHECK refuses a margin wider than half the band, which would latch a breach forever | pending | green |
| 2026-08-10 00:20 | S11.3 control-engine threshold evaluation with hysteresis; faulted AND stale readings do not evaluate — a dead probe is its own alert class | pending | green |
| 2026-08-10 00:20 | S11.4 api: threshold CRUD, GET /api/v1/alerts (active + recent), AlertFrame on the stream | pending | green |
| 2026-08-10 00:20 | Ruling: an unknown *stream frame kind* is skipped by clients, not fatal. Loud rejection stays the rule on the spine; refusing to render a temperature because the hub sent a newer frame type is worse than ignoring it | — | — |
| 2026-08-10 00:31 | S11.5 verified on hardware: band set via API, breach raised at 23.687 °C over a 20.0 max, cleared at 23.875 °C after the band widened | — | green |
| 2026-08-10 00:31 | Gap found, not closed: hardware-io publishes readings but never registers the sensor into Postgres, so thresholds cannot bind to a real probe without a manual devices row | — | — |
| 2026-08-10 00:55 | S12.1 artifacts/ added to .gitignore — scratch images must not enter a repo intended for AGPL publication. Note: artifacts/bellasreef-day1-wiring.pdf was already tracked before the rule and is still in history | `9c3dbba` | n/a |
| 2026-08-10 00:55 | S12.2 app icon vectorised from the source grid (front/ghost split by luminance, potrace); 99.19% / 99.47% IoU against the source masks; baked shadows dropped for Liquid Glass | ios `d667ec2` | green |
| 2026-08-10 00:55 | S12.3 layered .icon NOT wired: actool rejects a hand-authored icon.json with an identical nil-insert crash across three schema variants. No published schema, no sample in Xcode — finishing it is a GUI step, documented in Icon/README.md. Shipping a conventional appiconset meanwhile | — | — |
| 2026-08-10 03:10 | S13.1 /info gains approvers_available; DELETE /clients/me self-revoke. Signing out now reaches the hub — forgetting only locally left a row the hub counted as a live approver, which is the lockout David hit | pending | green |
| 2026-08-10 03:10 | S13.2 sensor registration on bellasreef.registry.>; BR_REGISTRY retained last-value-per-subject; API consumes and upserts. hardware-io stays Postgres-free | pending | green |
| 2026-08-10 03:10 | S13.3 registry consumer retries until the stream exists — hardware-io provisions it and nothing orders the two services, so subscribing once meant the devices table only populated if the API happened to start second | pending | green |
| 2026-08-10 03:10 | S13.4 migration 0007 devices.display_name + PATCH rename; upsert names its columns so a hardware re-announce cannot reset the operator's name or band | pending | green |
| 2026-08-10 03:10 | S13.5 /devices and /sensors typed (DeviceView) — they returned list[dict], which generates as an opaque container in Swift and forces hand-written key access | pending | green |
| 2026-08-10 03:21 | S13.6 verified: devices table emptied, services restarted, row recreated from the retained registration with zero manual SQL | — | green |
| 2026-08-10 03:21 | Design brief v1.2 §2: destructive controls follow iOS convention; safety-red governs status/data. Transcribed from David's message — his edit was not saved to disk | `ef3d88e` | n/a |
| 2026-08-10 10:05 | S14.1 end-to-end UI write test: rename + thresholds driven through the detail sheet, asserted by *reopening* the sheet so the values are hub state re-read over REST, not the text that was typed | ios `pending` | green |
| 2026-08-10 10:05 | S14.2 422 path proven through the UI; Pydantic's "Value error, " envelope stripped so the hub's sentence reaches the operator intact | ios `pending` | green |
| 2026-08-10 10:12 | S14.3 §7 review P1: System showed the IP under "Name" — HubMemory persisted only the URL and derived the name as url.host, which became an IP once discovery started resolving addresses. Name now comes from /info; the name is also persisted at pairing as a fallback | — | green |
| 2026-08-10 10:12 | S14.4 §7 review P1: remaining tab ghosting was `thermometer.medium`, which has no filled variant. It looks solid while selected, so it only ghosted when viewed from another tab — which is why the first fix appeared to work | — | green |
| 2026-08-10 10:12 | S14.5 §7 review P2/P3: Tank is one stack from the safe area (nav bar hidden, status inline, seam gone); threshold values tinted as editable; sparkline gained range + span derived from the probe's declared cadence | — | green |
| 2026-08-10 10:12 | Bug found while verifying P3: the status line is computed against Date(), and staleness arrives as an *absence* of frames — nothing mutated observed state, so it sat on teal "All clear" beside a dimmed reading stamped "1m ago". The staleness indicator had gone stale. Now on a 5s TimelineView | — | green |
| 2026-08-10 10:12 | Bug found: a connected hub with no probe reporting rendered the all-clear teal. An unmonitored tank is not healthy; tone is amber when nothing is reporting | — | green |
| 2026-08-10 11:05 | S15.0 VERIFY (task 1): nothing writes to VictoriaMetrics. No push path in any service, VM has no scrape config, and the container is not running on the Pi. Reported, not fixed | — | n/a |
| 2026-08-10 11:05 | S15.1 contracts 3.0.0: control_authority / failsafe_capable / transport required on ActuatorRegistration, no defaults (device-classes.md §2) | pending | green |
| 2026-08-10 11:05 | S15.2 §2.1 authoritative requires failsafe_capable, transport=local, and the full R1 triple; §2.2 advisory REJECTS a declared safe_state rather than ignoring it | pending | green |
| 2026-08-10 11:05 | S15.3 migration 0008: columns + backfill to authoritative/true/local; the blanket actuator triple constraint is replaced by an authoritative-scoped one; downgrade refuses while non-authoritative rows exist | pending | green |
| 2026-08-10 11:05 | S15.4 §2.3 observe_only closes the command path at the API boundary — createOverride returns 409, not a downstream filter | pending | green |
| 2026-08-10 11:05 | S15.5 hardware-io's supervisor now refuses any non-authoritative registration (§3), which is also what makes the safety-triple narrowing type-sound | pending | green |
| 2026-08-10 11:05 | **CONFLICT FLAGGED, NOT RESOLVED**: device-classes.md §2.2 contradicts PRD R1 ("Every actuator registration declares ...") and the CLAUDE.md rule mirroring it. Under both documents an advisory device is unregisterable — rejected by the PRD for lacking a triple and by §2.2 for carrying one. Implemented per David's explicit instruction; PRD/CLAUDE.md left untouched pending a ruling | — | — |

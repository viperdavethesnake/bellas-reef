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
| 2026-08-10 12:05 | S16.1 PRD R1 replaced (authority-scoped) and CLAUDE.md:76 scoped to match; conflict notes removed from migration 0008 and the R1 test | pending | green |
| 2026-08-10 12:05 | S16.2 §2.3 gap closed: observe_only rejects a declared safe_state, doc updated first, then wire + migration 0009 (constraint widened, not duplicated) | pending | green |
| 2026-08-10 12:05 | S16.3 telemetry writer in the API service → VM /api/v1/import. BR_TELEMETRY stream added so a writer restart does not punch holes in history; actuator state read from BR_STATE (NATS forbids overlapping stream subjects) | pending | green |
| 2026-08-10 12:05 | S16.4 verified on hardware: bellasreef_sensor_reading and bellasreef_alert_state queryable in a real VictoriaMetrics | — | green |
| 2026-08-10 12:05 | S16.5 CI: a skip for a missing environment now fails the gate (conftest.py); VictoriaMetrics added to CI services so the read-back test runs rather than skips | pending | green |
| 2026-08-10 12:05 | Departure from a literal §4: last-exchange age is a METRIC, not a label — a label changing every sample mints a series every sample, and "when did we stop knowing" has to be charted. command_acked stays a label (cardinality 2) | — | — |
| 2026-08-10 12:05 | ActuatorState gained optional command_acked / last_exchange_age_s. Without them §4's advisory labels are unreachable: extra="forbid" means the contract rejects the fields outright | — | — |
| 2026-08-10 12:05 | FLAGGED: §4 wants authority on alert episodes, but §2 confines control_authority to actuators and every alert today is on a sensor — so every episode labels not_applicable. Needs sensors to carry authority, or a transport-based label. Not resolved | — | — |
| 2026-08-10 12:05 | FLAGGED: CLAUDE.md principle 3 still states the unscoped R1 rule; AuditWriter is wired into no service, so BR_AUDIT is never drained to Postgres | — | — |
| 2026-08-10 12:05 | Learned: VictoriaMetrics `-search.latencyOffset` (30s) hides the newest samples from instant queries — a fresh write is invisible to /api/v1/query and looks like a failed write. Read back with /api/v1/export | — | — |
| 2026-08-10 13:20 | S17.1 alert series gain a `transport` label from the device row; control_authority stays and is truthfully not_applicable for sensor-sourced episodes. Required sensors to declare transport — migration 0010 + SensorRegistration.transport — since §2 had forbidden every authority column on a sensor | pending | green |
| 2026-08-10 13:20 | S17.2 CLAUDE.md principle 3 rescoped to match PRD R1 as amended | pending | n/a |
| 2026-08-10 13:20 | S17.3 AuditWriter moved hardware-io → api (it contradicted §3 where it sat), decoupled from hardware-io's Spine, wired into the lifecycle; GET /api/v1/audit added so the trail is checkable from outside the hub | pending | green |
| 2026-08-10 13:20 | S17.4 gate test: every declared background component must report `is_running` after the real lifespan starts. It immediately caught that my own `is_running` was wrong for push consumers — their setup task *completes* after subscribing, so "task alive" reports False on a healthy service | pending | green |
| 2026-08-10 13:20 | S17.5 verified on hardware: auth event → BR_AUDIT → audit_log → GET /api/v1/audit (pair.requested, token.minted, thresholds.set, client.revoked all persisted) | — | green |
| 2026-08-10 13:20 | Root cause of the recurring "filtered consumer not unique": a control-engine test created `ramp-<uuid>` on the BR_CMD workqueue and never deleted it. A workqueue permits ONE consumer per filter, so every test run left a durable that blocked hardware-io from starting. Cost three debugging detours before it was traced | pending | green |
| 2026-08-10 13:20 | Same leak in my own new test — a test-scoped audit durable blocked the hub's API until deleted by hand. Both now delete on teardown | pending | green |
| 2026-08-10 14:00 | S18.0 CLAUDE.md test standards: integration tests must delete every durable they create; env-skips fail the gate | pending | n/a |
| 2026-08-10 14:00 | S18.1 GET /api/v1/history — server-side downsampling with min/avg/max per bucket. A plain average loses the spike an alert was raised on, which would band a curve that never appears to breach | pending | green |
| 2026-08-10 14:00 | S18.2 gaps are absent buckets, never zero-filled or interpolated: BR_STATE is last-value-retained so duty genuinely has holes. Client segments on >1.5x bucket and breaks the line; the footnote names the gaps | pending | green |
| 2026-08-10 14:00 | S18.3 History tab: envelope band + mean line, alert-episode bands from raised_at/cleared_at (open episodes clamp to the window edge rather than inventing a clear time), range picker, all five §7.1 states | pending | green |
| 2026-08-10 14:00 | S18.4 §7.7: alert age formatter — `.relative(presentation: .numeric)` rendered "in 0 seconds" at breach. RelativeAge counts up only and clamps a future timestamp to "just now"; clocks on hub and phone need not agree | pending | green |
| 2026-08-10 14:00 | Caught in review of my own chart: Charts anchors a numeric axis at zero, squeezing a 77 °F trace into the top 5% of the plot. Temperature scales to its envelope; duty stays 0–100 because percent-of-full has a real zero | pending | green |
| 2026-08-10 14:00 | hardware-io died mid-session when I deleted its BR_CMD consumer out from under the running process — a JetStream fetch against a deleted consumer is unhandled and exits. Noted, not fixed | — | — |
| 2026-08-10 16:45 | S19.1 command consumer self-heals: on consumer-vanished log CRITICAL, re-provision (idempotent, in case the stream went too), re-subscribe with exponential backoff; a successful heal resets the budget | pending | green |
| 2026-08-10 16:45 | S19.2 bounded at 5 attempts (~15s), then raises ConsumerLostError so shutdown runs and the restart path — which asserts every actuator into its safe state — takes over. Retrying forever would leave a hub "running" and applying nothing, this project's recurring failure in another costume | pending | green |
| 2026-08-10 16:45 | S19.3 verified on the live hub: deleted BR_CMD's consumer under a running hardware-io; CRITICAL logged, recovered on attempt 1, process stayed up. The same action killed it earlier today | — | green |

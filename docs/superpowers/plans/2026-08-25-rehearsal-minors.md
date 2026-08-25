# Rehearsal Minors (F1, F3, F4, move-audit, actor-name) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close five queued minors from the 2026-08-24 factory-reset rehearsal and the 08-25 schedule-assignment session: F1 (import path not mounted in the api container), F3 ("Waiting for a sensor" assumes a probe is wanted), F4 (sensor banner leaks into the Lighting tab), the missing `schedule.unassigned` audit row when a channel moves between schedules, and audit actors recorded as bare client UUIDs.

**Architecture:** Two independent repos. Backend (`/Users/david/visualstudio/bellasreef`): two audit fixes in `services/api/bellasreef_api/app.py` + one store method, plus a read-only compose mount. iOS (`/Users/david/visualstudio/bellasreef-ios`): status-derivation changes in `BellasReefKit/Sources/BellasReefKit/TankMonitor.swift` (testable Kit logic) with thin view wiring in the app target. Each repo gets its own branch, PR, CI, merge; the backend additionally ends with `scripts/deploy-pi.sh` and telemetry verified on the wire — CI green alone is not done.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / pytest (backend); Swift 6 / SwiftUI / Swift Testing (iOS); Docker Compose (deploy).

**Spec:** `docs/drafts/2026-08-24-factory-reset-rehearsal.md` (findings F1, F3, F4 and the Checkpoint D actor observation) plus the 2026-08-25 session note "move writes no `schedule.unassigned` for the old schedule". Note: the deploy-pi.sh stray-`]` footnote from Checkpoint C is ALREADY FIXED (merged `e5e0074`, sed class `[^],]*`) — it is not in this plan.

## Global Constraints

- Backend: Python 3.13+, fully typed, `mypy --strict` clean, ruff lint/format, pytest. Run from repo root: `uv run --project services/api pytest services/api/tests -x -q` (check `services/api/pyproject.toml` for the exact runner; CI is the gate either way).
- Backend tests hitting Postgres use the loopback dev-container pattern already in the test files (`fresh_engine()` reads an env URL). NEVER point any test at the hub (`bellasreef.local`). If no local Postgres container is available, declare `BELLASREEF_ALLOW_ENV_SKIPS=1` and say so — CI is where they run.
- Conventional commits. PRs must pass CI; no direct pushes to main.
- Backend stop condition: CI green → `scripts/deploy-pi.sh` → telemetry verified on the wire. All three.
- iOS: regenerate the project with `xcodegen generate` before building; Kit tests via `cd BellasReefKit && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild test -scheme BellasReefKit-Package -skipPackagePluginValidation`. Client bindings are generated — never hand-write API types.
- Copy decisions taken in this plan (David may veto at review): a live hub with zero adopted sensors shows teal "No sensors adopted"; the Lighting tab's live status line reads "Connected".
- Audit rows are append-only: actor names are resolved at write time and are point-in-time truth; a later rename must not rewrite history (no change needed — just do not "fix" this).

---

## Part A — backend (`/Users/david/visualstudio/bellasreef`, branch `fix/rehearsal-minors` off `main`)

### Task 1: `schedule.unassigned` audit row when a channel moves between schedules

`assign_schedule` (app.py:2414) replaces whatever schedule the channel had, but writes only `schedule.assigned` for the new one. The old schedule's audit history shows the channel arriving and never leaving.

**Files:**
- Modify: `services/api/bellasreef_api/app.py:2442-2452` (inside `assign_schedule`)
- Test: `services/api/tests/test_schedules_api.py` (add after `test_assign_and_unassign_write_exactly_their_events`, line ~735)

**Interfaces:**
- Consumes: existing helpers in test_schedules_api.py — `fresh_engine()`, `seed_device(engine, device_id, authority)`, `Audit()`, `build_app(engine, audit=...)`, `paired(app)` (pairs a client named `"phone"`), `curve(name=...)`, `run(scenario)`.
- Produces: a `schedule.unassigned` audit event with keys `channel_id`, `schedule_id` (the OLD schedule), `moved_to` (the new schedule id), `actor`. Task 2 later reshapes actor fields; write this task against the current `"actor": str(actor)` form.

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/test_schedules_api.py`, next to `test_assign_and_unassign_write_exactly_their_events`:

```python
def test_moving_a_channel_writes_unassigned_for_the_old_schedule(self) -> None:
    """Reassigning a channel is a departure and an arrival, and the old
    schedule's history must show the departure (session note 2026-08-25).
    """

    async def scenario() -> Audit:
        engine = await fresh_engine()
        await seed_device(engine, "led-blue", "authoritative")
        audit = Audit()
        app = build_app(engine, audit=audit)
        headers = await paired(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            first = (
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
            ).json()
            second = (
                await c.post(
                    "/api/v1/lighting/schedules",
                    headers=headers,
                    json=curve(name="amber-dusk"),
                )
            ).json()
            await c.put(
                "/api/v1/lighting/channels/led-blue/schedule",
                headers=headers,
                json={"schedule_id": first["id"]},
            )
            await c.put(
                "/api/v1/lighting/channels/led-blue/schedule",
                headers=headers,
                json={"schedule_id": second["id"]},
            )
            self.first_id, self.second_id = first["id"], second["id"]
        await engine.dispose()
        return audit

    audit = run(scenario)
    assert audit.count("schedule.assigned") == 2
    assert audit.count("schedule.unassigned") == 1
    detail = audit.detail("schedule.unassigned")
    assert detail["channel_id"] == "led-blue"
    assert detail["schedule_id"] == self.first_id, "the row names the OLD schedule"
    assert detail["moved_to"] == self.second_id


def test_reassigning_the_same_schedule_writes_no_unassigned(self) -> None:
    """Idempotent re-assign is not a move; no phantom departure row."""

    async def scenario() -> Audit:
        engine = await fresh_engine()
        await seed_device(engine, "led-blue", "authoritative")
        audit = Audit()
        app = build_app(engine, audit=audit)
        headers = await paired(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            created = (
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
            ).json()
            for _ in range(2):
                await c.put(
                    "/api/v1/lighting/channels/led-blue/schedule",
                    headers=headers,
                    json={"schedule_id": created["id"]},
                )
        await engine.dispose()
        return audit

    audit = run(scenario)
    assert audit.count("schedule.assigned") == 2
    assert audit.count("schedule.unassigned") == 0
```

Check the `Audit` helper in this file for a `count` method; if it lacks one (test_alerts_api.py's copy does), add to this file's `Audit` class:

```python
    def count(self, event: str) -> int:
        return sum(1 for e, _, _ in self.records if e == event)
```

(Only if missing — line 727 already calls `audit.count`, so it likely exists here.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --project services/api pytest services/api/tests/test_schedules_api.py -k "moving_a_channel or reassigning_the_same" -x -q`
Expected: FAIL — `assert audit.count("schedule.unassigned") == 1` sees 0.

- [ ] **Step 3: Implement the move row in `assign_schedule`**

In `app.py`, inside `assign_schedule`, between the `observe_only` check and the `schedules.assign` call, look up the current holder (the same expression `unassign_schedule` uses at line 2467); after a successful assign, emit the departure row first, then the arrival:

```python
# A reassign is a departure and an arrival. Look the old holder up
# before the assign overwrites it, so the old schedule's history shows
# the channel leaving (rehearsal follow-up, 2026-08-25).
previous = next(
    (s.id for s in await schedules.list() if channel_id in s.assigned_channels),
    None,
)
try:
    await schedules.assign(channel_id, body.schedule_id)
except KeyError as exc:
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no schedule {body.schedule_id}") from exc
if previous is not None and previous != body.schedule_id:
    await sink(
        "schedule.unassigned",
        {
            "channel_id": channel_id,
            "schedule_id": str(previous),
            "moved_to": str(body.schedule_id),
            "actor": str(actor),
        },
        category="config",
    )
await sink(
    "schedule.assigned",
    {"channel_id": channel_id, "schedule_id": str(body.schedule_id), "actor": str(actor)},
    category="config",
)
```

(The `try/except` around `schedules.assign` already exists — the change is the `previous` lookup before it and the conditional `sink` after it. The failed-mutation rule holds: a 404 raises before any row is written.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --project services/api pytest services/api/tests/test_schedules_api.py -x -q`
Expected: PASS, including the two pre-existing assign/unassign tests (the plain first-assign path has `previous is None` and writes nothing new).

- [ ] **Step 5: Commit**

```bash
git add services/api/bellasreef_api/app.py services/api/tests/test_schedules_api.py
git commit -m "fix(api): moving a channel between schedules writes schedule.unassigned for the old one"
```

### Task 2: audit actors carry the client's resolved name, not a bare UUID

Rehearsal Checkpoint D: "`device.bound` actor is the client's bare UUID, not its resolved name." Every API-path config event has the same shape — `"actor": str(actor)` where `actor` is the `current_client` UUID. Fix uniformly: `actor` becomes the paired client's name (point-in-time), `actor_id` keeps the UUID.

**Files:**
- Modify: `services/api/bellasreef_api/store.py` (new method, place near `is_active`, ~line 841)
- Modify: `services/api/bellasreef_api/app.py` (helper after `current_client` ~line 991, then every payload carrying `"actor": str(actor)` — grep hits at lines 1457, 1629, 1684, 1757, 1820, 1875, 2084, 2204, 2215, 2250, 2314, 2364, 2397, 2450, 2476, plus the two Task-1 sites, plus `"revoked_by": str(actor)` at 1480)
- Test: `services/api/tests/test_schedules_api.py` (actor fields on `schedule.assigned`), `services/api/tests/test_device_binding.py` (actor fields on `device.bound` — the row the finding names)

**Interfaces:**
- Consumes: `paired_clients` table (`id`, `name` columns — see `store.list_clients`); `Depends(current_client)` yields the client UUID.
- Produces: `Store.client_name(client_id: UUID) -> str | None`; app-local `actor_fields(client_id: UUID) -> dict[str, str]` returning `{"actor": <name or uuid-string>, "actor_id": <uuid-string>}`. Audit payloads gain `actor_id`; `actor` becomes the name. `client.revoked` (admin path) gains `revoked_by_id` beside a resolved `revoked_by`.

- [ ] **Step 1: Write the failing tests**

In `services/api/tests/test_schedules_api.py`, add:

```python
def test_audit_actor_is_the_client_name_not_a_uuid(self) -> None:
    """Checkpoint D observation (rehearsal 2026-08-24): a bare UUID names
    nobody. `actor` carries the paired client's name; `actor_id` keeps the
    UUID for identity."""

    async def scenario() -> Audit:
        engine = await fresh_engine()
        await seed_device(engine, "led-blue", "authoritative")
        audit = Audit()
        app = build_app(engine, audit=audit)
        headers = await paired(app)  # pairs as "phone"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            created = (
                await c.post("/api/v1/lighting/schedules", headers=headers, json=curve())
            ).json()
            await c.put(
                "/api/v1/lighting/channels/led-blue/schedule",
                headers=headers,
                json={"schedule_id": created["id"]},
            )
        await engine.dispose()
        return audit

    audit = run(scenario)
    detail = audit.detail("schedule.assigned")
    assert detail["actor"] == "phone"
    uuid.UUID(detail["actor_id"])  # parses, or raises
```

(`import uuid` if the file lacks it — check the imports at the top.)

In `services/api/tests/test_device_binding.py`, add (uses that file's own helpers — `fresh_engine`, `announce`, `paired_client`, `run`, `ROM`):

```python
def test_device_bound_actor_is_the_client_name() -> None:
    """The rehearsal's exact row: device.bound carried a bare UUID actor."""

    async def scenario() -> tuple[Audit, str]:
        engine = await fresh_engine()
        await announce(engine, "w1-bus", ROM)
        audit = Audit()
        app = build_app(engine, audit=audit, nats_url=None, vm_url=None)
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")
        granted = (await c.post("/api/v1/pair", json={"client_name": "bench-mac"})).json()
        minted = await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
        headers = {"Authorization": f"Bearer {minted.json()['access_token']}"}
        await c.post(
            "/api/v1/devices",
            headers=headers,
            json={
                "device_id": "display-tank",
                "driver_type": "ds18b20",
                "channel": ROM,
                "poll_interval_s": 5.0,
            },
        )
        await c.aclose()
        await engine.dispose()
        return audit, str(granted["client_id"])

    audit, client_id = run(scenario)
    detail = audit.detail("device.bound")
    assert detail["actor"] == "bench-mac"
    assert detail["actor_id"] == client_id
```

(Match this file's actual `build_app` call signature and `run` helper — copy the shape of the neighboring tests if the kwargs differ.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --project services/api pytest services/api/tests/test_schedules_api.py::*audit_actor* services/api/tests/test_device_binding.py::test_device_bound_actor_is_the_client_name -x -q` (adjust selector syntax: `-k "actor_is_the_client_name"` works across both files)
Expected: FAIL — `detail["actor"]` is a UUID string, and `actor_id` is absent (KeyError).

- [ ] **Step 3: Add `Store.client_name`**

In `store.py`, next to `is_active`:

```python
    async def client_name(self, client_id: UUID) -> str | None:
        """The paired client's name, or None for a row that does not exist.

        Read at audit-write time: rows are append-only, so the name recorded
        is point-in-time truth and a later change never rewrites history.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT name FROM paired_clients WHERE id = :id"),
                    {"id": client_id},
                )
            ).first()
            return None if row is None else str(row[0])
```

- [ ] **Step 4: Add the app helper and sweep the emission sites**

In `app.py`, directly after the `current_client` dependency (~line 991):

```python
    async def actor_fields(client_id: UUID) -> dict[str, str]:
        """Resolved operator identity for audit payloads.

        The audit column shows ``actor`` verbatim, and a bare UUID names
        nobody (rehearsal 2026-08-24, checkpoint D). ``actor_id`` keeps the
        durable identity — names are the client's to choose, ids are not.
        """
        name = await store.client_name(client_id)
        return {"actor": name or str(client_id), "actor_id": str(client_id)}
```

Then sweep: `grep -n '"actor": str(actor)' services/api/bellasreef_api/app.py` and in every hit replace the key-value pair with a spread of the helper. Example (the `device.bound` site at ~line 1622):

```python
        await sink(
            "device.bound",
            {
                "device_id": device_id,
                "driver_type": body.driver_type,
                "channel": body.channel,
                "created": created,
                **await actor_fields(actor),
            },
            category="config",
        )
```

Special cases:
- `revoke_self` (~1457): `{"client_id": str(actor), "self": True, **await actor_fields(actor)}` — keep `client_id`, it names the subject.
- admin `client.revoked` (~1480): replace `"revoked_by": str(actor)` with `"revoked_by": (await store.client_name(actor)) or str(actor), "revoked_by_id": str(actor)` (this event's actor-shaped field has a different name; do not also add `actor`).
- Both Task-1 sites in `assign_schedule` (the move row and the assigned row) and the `unassign_schedule` site get the same `**await actor_fields(actor)` treatment.
- Do NOT touch events whose actor is not a client (the sink's own `"api"` source-fill, CLI-path events in `cli.py`, `pair.*` events carrying `client_name` already).

- [ ] **Step 5: Run the new tests, then the whole API suite**

Run: `uv run --project services/api pytest services/api/tests -x -q`
Expected: the two new tests PASS. Any pre-existing test that pinned `detail["actor"]` to a UUID will fail — update those assertions to the resolved-name + `actor_id` shape (they are asserting the bug). `test_background_components.py:367` (`actor == "api"`) must still pass untouched — those are system-emitted events.

- [ ] **Step 6: Types and lint**

Run: `uv run --project services/api mypy --strict services/api/bellasreef_api && uv run ruff check services/api && uv run ruff format --check services/api`
Expected: clean. (Use the repo's actual mypy/ruff invocations if CI names different ones — see `.github/workflows/`.)

- [ ] **Step 7: Commit**

```bash
git add services/api/bellasreef_api/app.py services/api/bellasreef_api/store.py services/api/tests/
git commit -m "fix(api): audit actors carry the client's resolved name; UUID moves to actor_id"
```

### Task 3: F1 — mount `/etc/bellasreef` read-only into the api container

The documented import command (`docker compose exec api bellasreef devices import /etc/bellasreef/devices.import.yaml`) fails because the path lives on the host only. The mount makes the documented command true as written; the alternative (rewriting the doc to a copy-in dance) was the rehearsal's workaround, not the fix.

**Files:**
- Modify: `deploy/compose.yaml` (api service `volumes:` block, currently just the backups mount)
- Modify: `docs/drafts/2026-08-24-factory-reset-rehearsal.md` (mark F1 resolved, dated)

**Interfaces:**
- Consumes: host directory `/etc/bellasreef` on the Pi (exists; verified during the rehearsal).
- Produces: `/etc/bellasreef` visible read-only inside the api container. No code change; no test — verified at deploy time in Task 4.

- [ ] **Step 1: Add the mount**

In `deploy/compose.yaml`, api service `volumes:`:

```yaml
    volumes:
      # `bellasreef backup --out /backups/...` lands archives on the host here.
      - /home/david/backups:/backups
      # Read-only: the device-import manifest the docs tell the operator to
      # pass lives here on the host, and the documented command runs inside
      # this container (rehearsal 2026-08-24, F1).
      - /etc/bellasreef:/etc/bellasreef:ro
```

- [ ] **Step 2: Validate the manifest**

Run: `docker compose -f deploy/compose.yaml config -q` (if Docker is unavailable on this Mac, note it and rely on CI/deploy; do not skip silently)
Expected: exit 0, no output.

- [ ] **Step 3: Mark F1 resolved in the rehearsal doc**

In `docs/drafts/2026-08-24-factory-reset-rehearsal.md`, append one sentence to the F1 bullet:

```
  RESOLVED 2026-08-25: `/etc/bellasreef` is now a read-only mount in the api
  service (deploy/compose.yaml), so the documented command works as written.
```

- [ ] **Step 4: Commit**

```bash
git add deploy/compose.yaml docs/drafts/2026-08-24-factory-reset-rehearsal.md
git commit -m "fix(deploy): mount /etc/bellasreef read-only into api so the documented import command works (rehearsal F1)"
```

### Task 4: backend PR, CI, merge, deploy, wire verification

**Files:** none new — process task.

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin fix/rehearsal-minors
gh pr create --title "fix: rehearsal minors — move audit row, resolved audit actors, /etc/bellasreef mount (F1)" --body "$(cat <<'EOF'
Closes three queued minors:

- `schedule.assigned` on an occupied channel now also writes `schedule.unassigned`
  for the old schedule (with `moved_to`), so a move shows as departure + arrival.
- Audit actors are the paired client's resolved name; the UUID moves to `actor_id`
  (rehearsal 2026-08-24 checkpoint D). `revoked_by` gets the same treatment.
- F1: `/etc/bellasreef` mounted read-only into the api container so the documented
  device-import command works as written.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Watch CI to green**

Run: `gh pr checks --watch`
Expected: all green. If red, fix before anything else. (Check `gh pr view --json mergeable` first if no check-suite appears — a conflicting PR gets no CI run at all.)

- [ ] **Step 3: Squash-merge**

Run: `gh pr merge --squash --delete-branch` (confirm the squash title matches the PR title — retitle before merging if the PR pivoted).

- [ ] **Step 4: Deploy and verify on the wire**

```bash
git checkout main && git pull
scripts/deploy-pi.sh
```

Expected: full ladder green ending `✓ deployed <sha> — API answering at contracts <ver>, fresh sample on the wire (<value>)` — and the value prints with NO stray `]` (first live confirmation of the e5e0074 sed fix).

- [ ] **Step 5: Verify F1 and the audit shape on the hub**

```bash
ssh bellasreef.local 'cd /home/david/bellasreef && docker compose -f deploy/compose.yaml exec api ls -la /etc/bellasreef/'
```
Expected: the directory lists (read-only mount live), including `devices.import.yaml`.

Then exercise one audited action from the iOS app or curl (e.g. rename a device or reassign a schedule) and read the newest audit row via `GET /api/v1/audit` — `actor` must be the client's name, `actor_id` the UUID. Report the actual row.

---

## Part B — iOS (`/Users/david/visualstudio/bellasreef-ios`, branch `fix/f3-f4-status` off `main`)

### Task 5: F3 — status keys on adopted sensors, not the probe stream

`TankMonitor.tone`/`statusLine` treat `probes.isEmpty` as "waiting", so a lighting-only hub (or the post-wipe empty registry) sits amber "Waiting for a sensor" forever. Direction ruled in the rehearsal doc: key on *adopted sensors*. The monitor stays REST-free — the registry count is injected as a closure, the same pattern as `cadenceOf`.

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/TankMonitor.swift` (~lines 152, 198, 218)
- Modify: `BellasReef/BellasReefApp.swift` (~line 108, beside the `cadenceOf` wiring)
- Test: `BellasReefKit/Tests/BellasReefKitTests/SensorlessStatusTests.swift` (create)

**Interfaces:**
- Consumes: `DeviceCatalog.sensors` (`[Components.Schemas.DeviceView]`, includes detached rows — filter `adopted == true`), `DeviceCatalog.state == .loaded`.
- Produces: `TankMonitor.adoptedSensorCount: () -> Int?` (public var, default `{ nil }`; nil = registry not loaded, current behavior applies). Task 6 does not depend on it.

- [ ] **Step 1: Write the failing tests**

Create `BellasReefKit/Tests/BellasReefKitTests/SensorlessStatusTests.swift`:

```swift
// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2026 Bella's Reef LLC
import Foundation
import Testing

@testable import BellasReefKit

/// Rehearsal 2026-08-24, F3: "Waiting for a sensor" assumed a temp probe is
/// wanted. A lighting-only hub — or the post-wipe empty registry — sat amber
/// forever. The line keys on *adopted sensors*, not on the probe stream.
@Suite("Status keys on adopted sensors, not the probe stream")
@MainActor
struct SensorlessStatusTests {
    private let hub = Hub(name: "hub", baseURL: URL(string: "http://hub.invalid:8000")!, discovered: false)

    /// A monitor driven to `.live` through the stream path, no probes reporting.
    private func liveMonitor() throws -> TankMonitor {
        let client = HubClient(hub: hub, tokens: MemoryCredentials(token: "t"),
                               transport: StubTransport { _, _, _ in (500, nil) })
        let monitor = TankMonitor(client: client, stream: StreamClient(baseURL: hub.baseURL))
        monitor.apply(try StreamClient(baseURL: hub.baseURL).decode(Fixtures.ready))
        return monitor
    }

    @Test("zero adopted sensors on a live hub is teal, not amber")
    func sensorlessHubIsAllClear() throws {
        let m = try liveMonitor()
        m.adoptedSensorCount = { 0 }
        #expect(m.tone == .allClear)
        #expect(m.statusLine == "No sensors adopted")
    }

    @Test("adopted sensors that have not reported still wait, amber")
    func adoptedButSilentStillWaits() throws {
        let m = try liveMonitor()
        m.adoptedSensorCount = { 1 }
        #expect(m.tone == .attention)
        #expect(m.statusLine == "Waiting for a sensor")
    }

    @Test("registry not loaded yet: the probe-stream fallback holds")
    func unknownRegistryFallsBack() throws {
        let m = try liveMonitor()
        // default adoptedSensorCount = { nil }
        #expect(m.tone == .attention)
        #expect(m.statusLine == "Waiting for a sensor")
    }
}
```

(Match `Hub`/`MemoryCredentials`/`StubTransport` construction to `StalenessTests.swift` in the same directory — copy its `monitor(cadence:)` shape if an initializer differs.)

- [ ] **Step 2: Run to verify the new suite fails**

Run: `cd BellasReefKit && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild test -scheme BellasReefKit-Package -skipPackagePluginValidation 2>&1 | tail -20`
Expected: `sensorlessHubIsAllClear` FAILS (compile error first — `adoptedSensorCount` undefined; then after stubbing, tone is `.attention`). The other two describe current behavior and pass once it compiles.

- [ ] **Step 3: Implement in TankMonitor**

Below `cadenceOf` (~line 152):

```swift
    /// Adopted-sensor count from the registry, injected like `cadenceOf` so
    /// the monitor stays a stream consumer that knows nothing about REST.
    /// nil means the registry has not loaded — the probe-stream fallback
    /// applies until it has.
    public var adoptedSensorCount: () -> Int? = { nil }
```

In `tone` (~line 198), replace:

```swift
        if probes.isEmpty { return .attention }
```

with:

```swift
        // A connected hub with no probe reporting is unmonitored ONLY if a
        // sensor is adopted and expected to report. Zero adopted sensors is a
        // configuration, not a fault — a lighting-only hub must not sit amber
        // forever (rehearsal F3). Unknown registry (nil) keeps the old rule.
        if probes.isEmpty && adoptedSensorCount() != 0 { return .attention }
```

In `statusLine` (~line 218), replace:

```swift
            if probes.isEmpty { return "Waiting for a sensor" }
```

with:

```swift
            if probes.isEmpty {
                return adoptedSensorCount() == 0 ? "No sensors adopted" : "Waiting for a sensor"
            }
```

- [ ] **Step 4: Wire the closure in the app target**

In `BellasReef/BellasReefApp.swift`, beside the `cadenceOf` line (~108):

```swift
        monitor.adoptedSensorCount = { [weak catalog] in
            guard let catalog, catalog.state == .loaded else { return nil }
            return catalog.sensors.filter { $0.adopted == true }.count
        }
```

(Check `DeviceView.adopted`'s optionality — `== true` compiles either way. Confirm `DeviceCatalog.Load` is Equatable for the `==` check; it is declared `Equatable` at DeviceCatalog.swift:21.)

- [ ] **Step 5: Run the Kit suite**

Run: `cd BellasReefKit && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild test -scheme BellasReefKit-Package -skipPackagePluginValidation 2>&1 | tail -5`
Expected: all tests PASS (224 existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add BellasReefKit/Sources/BellasReefKit/TankMonitor.swift BellasReef/BellasReefApp.swift BellasReefKit/Tests/BellasReefKitTests/SensorlessStatusTests.swift
git commit -m "fix(status): key the tank status on adopted sensors, not the probe stream (rehearsal F3)"
```

### Task 6: F4 — the Lighting tab gets a connection-scoped status line

`LightingView` reuses `monitor.tone`/`statusLine` wholesale, so sensor copy ("Waiting for a sensor", "Sensor fault") leaks into a tab about lights. The tab's status keeps what the 2026-08-15 review wanted — connection honesty and the interlock — and drops sensor states.

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/TankMonitor.swift` (add `connectionTone`/`connectionLine` below `statusLine`, ~line 223)
- Modify: `BellasReef/Views/TankView.swift:132-183` (`StatusLine` view gains a scope)
- Modify: `BellasReef/Views/LightingView.swift:66` (pass the scope)
- Test: `BellasReefKit/Tests/BellasReefKitTests/SensorlessStatusTests.swift` (extend)

**Interfaces:**
- Consumes: `TankMonitor.connection`, `TankMonitor.channels` (latched flag), `HealthTone` (`.allClear/.attention/.safety`).
- Produces: `TankMonitor.connectionTone: HealthTone`, `TankMonitor.connectionLine: String`; `StatusLine.Scope` enum (`.tank` default, `.connection`).

- [ ] **Step 1: Write the failing tests**

Append to `SensorlessStatusTests.swift` (same file — both findings are about what the status line claims):

```swift
    @Test("the connection-scoped line is blind to sensors")
    func connectionScopeIgnoresSensors() throws {
        let m = try liveMonitor()
        m.adoptedSensorCount = { 1 }        // tank scope would say "Waiting for a sensor"
        #expect(m.connectionTone == .allClear)
        #expect(m.connectionLine == "Connected")
    }

    @Test("the connection-scoped line still surfaces a latched interlock")
    func connectionScopeKeepsTheInterlock() throws {
        let m = try liveMonitor()
        m.apply(try StreamClient(baseURL: hub.baseURL).decode(Fixtures.latchedState))
        #expect(m.connectionTone == .safety)
        #expect(m.connectionLine == "Interlock latched")
    }

    @Test("a dead socket is amber in connection scope")
    func connectionScopeIsHonestAboutTheSocket() {
        let client = HubClient(hub: hub, tokens: MemoryCredentials(token: "t"),
                               transport: StubTransport { _, _, _ in (500, nil) })
        let m = TankMonitor(client: client, stream: StreamClient(baseURL: hub.baseURL))
        // never driven live
        #expect(m.connectionTone == .attention)
        #expect(m.connectionLine == "Not connected")
    }
```

For `Fixtures.latchedState`: check `FrameDecodingTests.swift` for an existing state-frame fixture with `latched: true`; if none exists, add one to the `Fixtures` enum modeled on its `state` fixture with `"latched":true` in the payload. If building a latched fixture turns into archaeology, drop that one test case and note it in the commit — the latch branch is a straight copy of the audited `tone` code.

- [ ] **Step 2: Run to verify failure**

Run: the Kit test command from Task 5.
Expected: compile FAILS — `connectionTone` undefined.

- [ ] **Step 3: Implement in TankMonitor**

Below `statusLine` (~line 223):

```swift
    /// Connection-scoped status for control surfaces (rehearsal F4). The
    /// Lighting tab needs the socket's honesty and the interlock — "Sensor
    /// fault" there describes a different screen's problem. Sensor states
    /// stay in `tone`/`statusLine`, which remain the Tank tab's pair.
    public var connectionTone: HealthTone {
        if channels.values.contains(where: { $0.payload.latched == true }) { return .safety }
        switch connection {
        case .live: return .allClear
        default: return .attention
        }
    }

    public var connectionLine: String {
        if channels.values.contains(where: { $0.payload.latched == true }) {
            return "Interlock latched"
        }
        switch connection {
        case .idle: return "Not connected"
        case .connecting: return "Connecting…"
        case .live: return "Connected"
        case let .disconnected(why): return "Disconnected — \(why)"
        case let .contractMismatch(detail): return "App and hub disagree — \(detail)"
        }
    }
```

- [ ] **Step 4: Scope the StatusLine view and switch LightingView**

In `TankView.swift`, `StatusLine`:

```swift
struct StatusLine: View {
    /// Which of the monitor's two voices this instance speaks with. The Tank
    /// tab reads the sensor-aware pair; a control surface reads the
    /// connection-scoped pair (rehearsal F4).
    enum Scope { case tank, connection }

    let monitor: TankMonitor
    /// Where the coverage note comes from. Optional so a caller without a
    /// catalog still gets the plain line.
    var catalog: DeviceCatalog? = nil
    var scope: Scope = .tank

    private var tone: HealthTone {
        scope == .tank ? monitor.tone : monitor.connectionTone
    }
    private var baseLine: String {
        scope == .tank ? monitor.statusLine : monitor.connectionLine
    }
```

Then in the existing `text` and `line` properties, replace every `monitor.tone` with `tone` and `monitor.statusLine` with `baseLine` (the coverage note in `text` already guards on `tone == .allClear` and `catalog != nil`, so connection scope with no catalog skips it naturally; also update the `.symbolEffect(..., value:)` and color references to the local `tone`).

In `LightingView.swift:66`:

```swift
                StatusLine(monitor: monitor, scope: .connection)
```

(Drop the `catalog:` argument — the coverage note is a sensor concern. Update the comment above it: the line is connection-scoped on purpose, rehearsal F4.)

- [ ] **Step 5: Run Kit tests, then build the app**

Run: Kit test command; then `xcodegen generate && xcodebuild -project BellasReef.xcodeproj -scheme BellasReef -destination 'platform=iOS Simulator,name=iPhone 17' build 2>&1 | tail -5` (match the scheme/destination the repo's docs or last session used).
Expected: tests PASS; app target builds (the app target has no unit tests — the build is the check that the view-layer changes compile).

- [ ] **Step 6: Commit**

```bash
git add BellasReefKit/Sources/BellasReefKit/TankMonitor.swift BellasReef/Views/TankView.swift BellasReef/Views/LightingView.swift BellasReefKit/Tests/BellasReefKitTests/SensorlessStatusTests.swift
git commit -m "fix(lighting): connection-scoped status line — sensor copy stays on the Tank tab (rehearsal F4)"
```

### Task 7: iOS PR, CI, merge, sim install

- [ ] **Step 1: Push, open PR, watch CI**

```bash
git push -u origin fix/f3-f4-status
gh pr create --title "fix: status keys on adopted sensors (F3); Lighting tab gets a connection-scoped line (F4)" --body "$(cat <<'EOF'
Rehearsal 2026-08-24 findings F3 and F4:

- A live hub with zero adopted sensors shows teal "No sensors adopted" instead of
  sitting amber "Waiting for a sensor" forever. Registry count injected into
  TankMonitor as a closure (same pattern as cadenceOf); nil keeps old behavior.
- The Lighting tab's StatusLine is connection-scoped: socket honesty + interlock,
  no sensor copy. Tank tab unchanged.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr checks --watch
```

Expected: green. Retitle before squash if anything pivots mid-PR (#19 lesson).

- [ ] **Step 2: Squash-merge, rebuild, install on the sim**

```bash
gh pr merge --squash --delete-branch
git checkout main && git pull
xcodegen generate
```

Build and install to the booted iPhone 17 sim (`9438872C…`, the paired client) the same way the last walkthrough build was installed (see `build/walkthrough/` convention). Expected on the sim against the live hub: Tank tab teal "No sensors adopted" only if the hub had no adopted sensors (it currently has `ds18b20` adopted, so expect normal behavior); Lighting tab status reads "Connected" instead of any sensor copy. Report what is actually on screen.

---

## Self-review notes

- Spec coverage: F1 → Task 3; F3 → Task 5; F4 → Task 6; move-audit → Task 1; bare-UUID actor → Task 2; Checkpoint C's stray `]` → already merged (`e5e0074`), verified live in Task 4 Step 4. F2 (`paired_client_count` naming) is deliberately out — it is an API-naming observation with load-bearing semantics, not queued work.
- Deliberately not included: F6/F8 on-device walkthrough (David's hands), Stages 4–6 (David's go), iOS #19 squash title (immutable history).
- Task 2's sweep count (~17 sites) is from `grep -n '"actor": str(actor)'` on 2026-08-25; re-grep at execution time, the file moves.

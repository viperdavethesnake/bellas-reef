# Live Adoption, Audit Categories, and New-Owner Experience — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make adoption take effect while hardware-io is running, make the audit
log say what happened, give unadopted devices re-add/forget lifecycle, and
implement the approved new-owner spec (setup code + factory-reset script) —
shipping as contracts 3.7.0.

**Architecture:** hardware-io keeps its single build-at-startup topology path
and gains a restart-on-assignment-change trigger (David's ruling 2026-08-15:
the drilled restart path IS the rebuild path; a ~15 s safe-state blip on the
rare adoption event is accepted). The audit sink stops hardcoding category
`auth` and the API promotes the event name to a typed field. Unbind stays soft;
two new endpoints (`readopt`, `forget`) complete the detached-device
lifecycle. Setup code + factory reset follow the approved spec verbatim.

**Tech Stack:** Python 3.13, FastAPI + Pydantic v2, NATS core/JetStream,
PostgreSQL 17 + Alembic, pytest, bash (scripts).

**Spec:** `docs/superpowers/specs/2026-08-15-new-owner-experience-design.md`
(features 1 and 3; feature 2 is the iOS repo's plan). The four UX fixes argue
from the 2026-08-15 triage in this session, recorded in each task.

## Global Constraints

- `mypy --strict` clean, Ruff clean; run `./scripts/check.sh` before every commit (`BELLASREEF_ALLOW_ENV_SKIPS=1` on this Mac — no container runtime).
- Integration tests never touch the hub (CLAUDE.md environment boundary). Loopback dev containers or CI only.
- Contract changes are ONE semver-minor bump: 3.6.x → **3.7.0**, regenerated once in Task 7. Earlier tasks change server surface but do not bump the version — check.sh's drift gate is satisfied at Task 7, so Tasks 2–5 commit with `--no-verify` is NOT allowed; instead run the regen in each task if check.sh demands it, but the version number moves once, in Task 7.
  - If `./scripts/check.sh` fails on spec drift in Tasks 2–5: run `python scripts/export-openapi.py` and include the regenerated artifact in that task's commit. The *version* still bumps only in Task 7.
- Conventional commits. PR against `main`; no direct pushes.
- Every actuator-affecting behavior keeps the safety architecture: startup asserts safe state; nothing here weakens that.
- The bench boundary applies: no electrical reasoning anywhere, including comments.

---

### Task 1: hardware-io exits (to rebuild) when an assignment changes

The 2026-08-15 root cause: `HardwareIO._build_from_registry` (app.py:369) runs
once; adoptions published later are never seen (control-engine subscribes to
`subjects.ALL_ASSIGNMENTS`; hardware-io does not). Ruled fix: on any assignment
event, log and stop cleanly. `restart: unless-stopped` restarts the container
(~15 s, measured in the drill), and startup rebuilds from the retained
registry — the code path the fail-safe drills already prove.

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/spine.py` (add `watch_assignments`)
- Modify: `services/hardware_io/bellasreef_hardware_io/app.py` (wire watcher after build, ~line 375)
- Test: `services/hardware_io/tests/test_assignment_watch.py` (create)

**Interfaces:**
- Consumes: `Spine._nc` (nats client, spine.py:129), `subjects.ALL_ASSIGNMENTS` (contracts), `HardwareIO.request_stop()` (app.py:347).
- Produces: `Spine.watch_assignments(on_event: Callable[[], None]) -> None` — core-NATS subscribe; every message on `bellasreef.assignment.>` invokes `on_event()`. No payload parsing: any assignment traffic means the registry moved.

- [ ] **Step 1: Write the failing test**

```python
# services/hardware_io/tests/test_assignment_watch.py
"""An assignment event after startup must stop the service so the restart
policy rebuilds it from the retained registry. One topology path, by ruling."""

import asyncio

import pytest

from bellasreef_hardware_io.app import HardwareIO


@pytest.mark.asyncio
async def test_assignment_event_requests_stop() -> None:
    service = HardwareIO(nats_url=None)  # match the ctor test_health.py uses
    assert not service._stopping.is_set()
    service._on_assignment_changed()
    assert service._stopping.is_set()


@pytest.mark.asyncio
async def test_second_event_is_harmless() -> None:
    service = HardwareIO(nats_url=None)
    service._on_assignment_changed()
    service._on_assignment_changed()  # burst of adoptions: one restart, no error
    assert service._stopping.is_set()
```

Adapt the constructor call to match how `test_health.py` builds a `HardwareIO`
(same fixtures, same kwargs) — the test's substance is the two asserts.

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd services/hardware_io && uv run pytest tests/test_assignment_watch.py -v`
Expected: FAIL — `AttributeError: _on_assignment_changed`

- [ ] **Step 3: Implement**

In `spine.py`, on the `Spine` class (near `read_assignments`, line 219):

```python
async def watch_assignments(self, on_event: Callable[[], None]) -> None:
    """Core subscribe to live assignment traffic.

    Deliberately payload-blind: any message here means the registry moved,
    and the response — rebuild via restart — is the same regardless of
    which device moved. Retained JetStream state is NOT redelivered on a
    core subscription, so startup's own read never triggers this.
    """
    if self._nc is None:
        raise RuntimeError("spine not connected")

    async def _cb(msg: Msg) -> None:
        on_event()

    await self._nc.subscribe(subjects.ALL_ASSIGNMENTS, cb=_cb)
    log.info("watching assignments", extra={"subject": subjects.ALL_ASSIGNMENTS})
```

In `app.py`, add the callback to `HardwareIO`:

```python
def _on_assignment_changed(self) -> None:
    """The registry moved under us. Exit cleanly; the restart policy
    rebuilds from the retained registry — the drilled path (ruled
    2026-08-15, restart-on-change over a live add/remove path)."""
    if not self._stopping.is_set():
        log.info(
            "assignment changed; exiting to rebuild from registry",
            extra={"event": "assignment_restart"},
        )
    self.request_stop()
```

Wire it in the startup sequence right after `_build_from_registry()`
(app.py:369) and before `_announce_capabilities()` — subscribe-after-build
means the initial retained read cannot re-trigger it:

```python
await self._build_from_registry()
if self.spine is not None:
    await self.spine.watch_assignments(self._on_assignment_changed)
await self._announce_capabilities()
```

- [ ] **Step 4: Run the test and full service suite**

Run: `cd services/hardware_io && uv run pytest -v`
Expected: new tests PASS, no regressions.

- [ ] **Step 5: Run repo checks and commit**

```bash
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh
git add services/hardware_io
git commit -m "fix(hardware-io): exit to rebuild when an assignment changes

Adoptions published while the service ran were never seen: assignments
were read once at startup. Any live assignment event now stops the
service cleanly and the restart policy rebuilds from the retained
registry - the single, drilled topology path. Ruled 2026-08-15."
```

---

### Task 2: audit events carry their real category, and the API exposes `action`

Every sink call publishes to `bellasreef.audit.auth` (`AUDIT_CATEGORY`,
audit.py:38) although `audit_log` supports
`{command, config, auth, state, safety, calibration}` — which is why every app
row reads "auth · api · bellasreef.audit.auth". The event name
(`device.unbound`, …) rides inside the `event` JSONB but clients get no typed
field for it.

**Files:**
- Modify: `services/api/bellasreef_api/audit.py` (sink gains `category` param)
- Modify: `services/api/bellasreef_api/app.py` (sink call sites get categories; `AuditEvent` gains `action`; `list_audit` fills it; `AuditSink` type alias updated)
- Modify: `services/api/bellasreef_api/cli.py` (`_emit` passes category where it emits `client.revoked` / pair events — same mapping)
- Test: `services/api/tests/test_audit_writer.py` (extend), `services/api/tests/test_auth_lifecycle.py` (extend where it asserts audit publishes)

**Interfaces:**
- Consumes: `subjects.audit(category)` (contracts, subjects.py:138); `_VALID_CATEGORIES` (audit_writer.py:63) — the writer already derives category from the subject's last token and normalizes unknowns, so no writer change is needed.
- Produces: `NatsAuditSink.__call__(event: str, detail: dict[str, Any], category: str = "auth")`; `AuditEvent.action: str | None` (API response model). Category mapping, fixed here and reused by iOS: `device.bound`, `device.unbound`, `device.renamed`, `thresholds.set`, and the Task 3 `device.forgotten` → `config`; `override.created`, `override.released` → `command`; all pairing/token/client events stay `auth`.

- [ ] **Step 1: Write the failing tests**

Extend `test_audit_writer.py` (or the test file that exercises
`NatsAuditSink`) with:

```python
@pytest.mark.asyncio
async def test_sink_publishes_to_the_event_category(fake_js_sink) -> None:
    sink, published = fake_js_sink  # follow the file's existing fake pattern
    await sink("device.unbound", {"device_id": "pi-pwm-0"}, category="config")
    subject, payload = published[-1]
    assert subject == "bellasreef.audit.config"


@pytest.mark.asyncio
async def test_sink_defaults_to_auth(fake_js_sink) -> None:
    sink, published = fake_js_sink
    await sink("token.minted", {"client_id": "x"})
    subject, _ = published[-1]
    assert subject == "bellasreef.audit.auth"
```

And in the API-level tests (wherever `list_audit` is exercised —
`test_auth_lifecycle.py` has the recent-audit assertions):

```python
async def test_audit_event_exposes_action(api_client_with_audit_row) -> None:
    # arrange one persisted row whose event JSONB is
    # {"event": "device.unbound", ...} using the file's existing helper
    events = (await api_client_with_audit_row.get("/api/v1/audit")).json()
    assert events[0]["action"] == "device.unbound"
```

Follow each file's existing fixture idioms; the asserts are the substance.

- [ ] **Step 2: Run to verify failure**

Run: `cd services/api && uv run pytest tests/test_audit_writer.py tests/test_auth_lifecycle.py -v`
Expected: FAIL — unexpected keyword `category` / KeyError `action`.

- [ ] **Step 3: Implement**

`audit.py`: change the signature and subject line; delete the constant's use
as the only subject (keep it as the default):

```python
async def __call__(
    self, event: str, detail: dict[str, Any], category: str = AUDIT_CATEGORY
) -> None:
    ...
    await js.publish(
        subjects.audit(category),
        json.dumps(payload).encode(),
        headers={"Nats-Msg-Id": message_id},
    )
```

`app.py`: update the `AuditSink` type alias and `_noop_audit` (line 97) to the
three-arg shape, then add `category="config"` at the `device.bound` (1229),
`device.renamed` (1287), `device.unbound` (1352), `thresholds.set` (1543) call
sites and `category="command"` at `override.created` (1646) and
`override.released` (1678). Pairing/token/revoke sites stay unchanged
(default `auth`).

`AuditEvent` (app.py:195) gains:

```python
    #: The event name from the payload ("device.unbound", "client.paired"),
    #: promoted to a typed field so clients render verbs, not subjects.
    action: str | None
```

`list_audit` (app.py:1369) fills it from the JSONB:

```python
        return [
            AuditEvent(**row, action=(row["event"] or {}).get("event"))
            for row in await store.recent_audit(limit=limit, category=category)
        ]
```

(match the existing construction style at that site; the substance is
`action=(row["event"] or {}).get("event")`).

`cli.py` `_emit` (line 76): same optional `category` parameter, defaulting to
`"auth"` — its current callers are all auth events, so call sites stay as
they are.

- [ ] **Step 4: Run the API suite**

Run: `cd services/api && uv run pytest -v`
Expected: PASS. If check.sh's drift gate complains, run
`python scripts/export-openapi.py` and stage the artifact (version bump still
waits for Task 7).

- [ ] **Step 5: Commit**

```bash
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh
git add services/api scripts contracts
git commit -m "feat(api): audit events carry their real category and a typed action

Every event published to bellasreef.audit.auth regardless of what it
was; device lifecycle now lands in config, overrides in command, and
AuditEvent.action promotes the event name out of the JSONB so clients
can say 'Unadopted Pretty Blue' instead of echoing the subject."
```

---

### Task 3: detached-device lifecycle — `adopted` on DeviceView, `readopt`, `forget`

Ruled 2026-08-15: unbound devices appear in a Detached section with re-add and
clear. Unbind already keeps the row + binding (store.py:391, `adopted = false`
is the whole mechanism). The API needs to (a) say which rows are detached,
(b) rebind one server-side from its stored binding, (c) hard-delete one on
explicit request.

**Files:**
- Modify: `services/api/bellasreef_api/app.py` (DeviceView field + two endpoints)
- Modify: `services/api/bellasreef_api/store.py` (`list_devices` returns `adopted`; add `readopt_device`, `forget_device`)
- Test: `services/api/tests/test_device_binding.py` (extend)

**Interfaces:**
- Consumes: `store.unadopt_device` semantics (row survives, binding kept); assignment publisher (`assignments.publish(DeviceAssignment(...))`, the app.py:1338 pattern); audit sink from Task 2.
- Produces:
  - `DeviceView.adopted: bool` (additive; `channel` semantics unchanged — still `None` when released).
  - `POST /api/v1/devices/{device_id}/readopt` → 200 `DeviceView` | 404 (unknown or not detached) | 409 (its channel is now held by another adopted device). Publishes `DeviceAssignment(adopted=True, ...)` from the stored row; audits `device.bound` with `{"readopt": true}`, category `config`.
  - `POST /api/v1/devices/{device_id}/forget` → 204 | 404 (unknown) | 409 (still adopted — unbind first). Hard-deletes the row; audits `device.forgotten`, category `config`. No assignment publish: a detached device holds no channel claim, and the unbind tombstone already recorded the release.
  - `Store.readopt_device(device_id) -> dict | None` (returns the re-adopted row's fields, `None` if not found/not detached; raises `ChannelHeldError(holder_id)` on conflict). `Store.forget_device(device_id) -> Literal["forgotten", "adopted", "missing"]`.

- [ ] **Step 1: Write the failing tests**

In `test_device_binding.py`, following its existing bind/unbind test idioms:

```python
async def test_device_view_reports_adopted(bound_device_client) -> None:
    devices = (await bound_device_client.get("/api/v1/devices")).json()
    assert all(d["adopted"] is True for d in devices)


async def test_unbound_device_is_listed_detached(bound_device_client) -> None:
    await bound_device_client.delete("/api/v1/devices/pi-pwm-0")
    devices = (await bound_device_client.get("/api/v1/devices")).json()
    row = next(d for d in devices if d["device_id"] == "pi-pwm-0")
    assert row["adopted"] is False and row["channel"] is None


async def test_readopt_restores_the_binding(bound_device_client) -> None:
    await bound_device_client.delete("/api/v1/devices/pi-pwm-0")
    r = await bound_device_client.post("/api/v1/devices/pi-pwm-0/readopt")
    assert r.status_code == 200 and r.json()["adopted"] is True
    assert r.json()["channel"] is not None  # same channel as before


async def test_readopt_conflicts_when_channel_taken(bound_device_client) -> None:
    # unbind pi-pwm-0, bind a NEW device onto the same channel via
    # POST /api/v1/devices (the file shows how), then:
    r = await bound_device_client.post("/api/v1/devices/pi-pwm-0/readopt")
    assert r.status_code == 409


async def test_forget_deletes_only_detached(bound_device_client) -> None:
    assert (
        await bound_device_client.post("/api/v1/devices/pi-pwm-0/forget")
    ).status_code == 409  # still bound
    await bound_device_client.delete("/api/v1/devices/pi-pwm-0")
    assert (await bound_device_client.post("/api/v1/devices/pi-pwm-0/forget")).status_code == 204
    devices = (await bound_device_client.get("/api/v1/devices")).json()
    assert all(d["device_id"] != "pi-pwm-0" for d in devices)
    assert (await bound_device_client.post("/api/v1/devices/pi-pwm-0/forget")).status_code == 404
```

NOTE (from session-log 2026-08-12): `Store.bind_device` matches an existing
row on `(driver_type, channel)` regardless of `adopted` — so binding a "new"
device onto a freed channel resurrects the OLD row and discards the proposed
id. The 409-conflict test must therefore create the competing device on a
*different* channel first and move it, or use a driver_type where a fresh row
is possible; read `bind_device` before writing this test and follow what it
actually permits. If a genuine conflict is unreachable by construction,
assert THAT instead (readopt after the channel was re-adopted returns 200 on
the same row) and say so in the test's docstring.

- [ ] **Step 2: Run to verify failure**

Run: `cd services/api && uv run pytest tests/test_device_binding.py -v`
Expected: FAIL — `adopted` missing, 404s on the new routes.

- [ ] **Step 3: Implement store methods**

In `store.py` near `unadopt_device` (line 367):

```python
class ChannelHeldError(Exception):
    def __init__(self, holder: str) -> None:
        self.holder = holder


async def readopt_device(self, device_id: str) -> dict[str, Any] | None:
    """Re-adopt a detached device onto its remembered binding.

    The inverse of unadopt_device, with the same soft philosophy: the row
    never moved, so identity and history reattach by construction. Fails
    loudly if another adopted device now holds the channel.
    """
    async with self._engine.begin() as conn:
        target = (
            (
                await conn.execute(
                    text(
                        "SELECT device_id, driver_type, binding FROM devices "
                        " WHERE device_id = :device_id AND NOT adopted"
                    ),
                    {"device_id": device_id},
                )
            )
            .mappings()
            .first()
        )
        if target is None:
            return None
        holder = (
            await conn.execute(
                text(
                    "SELECT device_id FROM devices "
                    " WHERE driver_type = :driver_type AND binding = :binding "
                    "   AND adopted AND device_id <> :device_id"
                ),
                {**target},
            )
        ).first()
        if holder is not None:
            raise ChannelHeldError(holder[0])
        row = (
            (
                await conn.execute(
                    text(
                        "UPDATE devices SET adopted = true "
                        " WHERE device_id = :device_id "
                        " RETURNING device_id, kind, role, driver_type, binding"
                    ),
                    {"device_id": device_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def forget_device(self, device_id: str) -> str:
    """Hard delete, detached rows only. The one sanctioned identity break:
    the operator is saying this hardware is gone for good."""
    async with self._engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT adopted FROM devices WHERE device_id = :device_id"),
                {"device_id": device_id},
            )
        ).first()
        if row is None:
            return "missing"
        if row[0]:
            return "adopted"
        await conn.execute(
            text("DELETE FROM devices WHERE device_id = :device_id"), {"device_id": device_id}
        )
        return "forgotten"
```

Adjust the binding-equality comparison to match how `binding` is stored
(JSONB: compare with `binding = CAST(:binding AS JSONB)` or on the extracted
channel key — read `bind_device`'s own matching SQL and use the identical
predicate). Add `adopted` to the `list_devices` SELECT (store.py:570).

- [ ] **Step 4: Implement endpoints**

In `app.py`, after `unbind_device` (line 1290 block):

```python
@app.post(
    "/api/v1/devices/{device_id}/readopt",
    tags=["hardware"],
    operation_id="readoptDevice",
    responses={
        200: {"description": "Re-adopted onto its remembered channel."},
        401: AUTH_401,
        404: {"description": "No such device, or it is not detached."},
        409: {"description": "Its channel is now held by another device."},
    },
)
async def readopt_device(device_id: str, _: Annotated[UUID, Depends(current_client)]) -> DeviceView:
    """Reattach a detached device to the channel its row still remembers."""
    try:
        row = await store.readopt_device(device_id)
    except ChannelHeldError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"channel now held by {exc.holder!r}. Unbind it first."
        ) from exc
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no detached device {device_id!r}.")
    if assignments is not None:
        await assignments.publish(
            DeviceAssignment(
                message_id=uuid4(),
                emitted_at=datetime.now(UTC),
                source="api",
                device_id=row["device_id"],
                adopted=True,
                role=row["role"],
                driver_type=row["driver_type"],
                binding=row["binding"],
            )
        )
    await sink("device.bound", {"device_id": device_id, "readopt": True}, category="config")
    full = next(d for d in await store.list_devices() if d["device_id"] == device_id)
    return DeviceView.model_validate(full)


@app.post(
    "/api/v1/devices/{device_id}/forget",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["hardware"],
    operation_id="forgetDevice",
    responses={
        204: {"description": "Deleted. Identity and settings are gone."},
        401: AUTH_401,
        404: {"description": "No such device."},
        409: {"description": "Still adopted. Unbind it first."},
    },
)
async def forget_device(device_id: str, _: Annotated[UUID, Depends(current_client)]) -> Response:
    """Delete a detached device row for good.

    The soft-unbind docstring explains why deletion is normally wrong: it
    severs history from hardware. This endpoint is the operator overruling
    that on purpose — the hardware is gone, the name should stop appearing.
    Telemetry already written keeps its device_id; nothing rewrites history.
    """
    outcome = await store.forget_device(device_id)
    if outcome == "missing":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no device {device_id!r}.")
    if outcome == "adopted":
        raise HTTPException(status.HTTP_409_CONFLICT, f"{device_id!r} is adopted. Unbind it first.")
    await sink("device.forgotten", {"device_id": device_id}, category="config")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

Add `adopted: bool` to `DeviceView` (app.py:275 block), documented:

```python
    #: False for a detached row: unbound, channel released, history kept.
    #: Clients section on this, not on channel being null.
    adopted: bool
```

- [ ] **Step 5: Run, check, commit**

Run: `cd services/api && uv run pytest -v` then
`BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` (regen spec artifact if the
drift gate asks).

```bash
git add services/api contracts
git commit -m "feat(api): detached-device lifecycle - adopted flag, readopt, forget

Unbind keeps the row by design; now clients can see that (DeviceView.
adopted), reattach it server-side from the remembered binding (409 if
the channel moved on), or delete it for good once detached. Ruled
2026-08-15: Detached section with re-add and clear."
```

---

### Task 4: migration 0017 + setup-code primitives

Spec Feature 1, storage and pure logic only (endpoint wiring is Task 5).

**Files:**
- Create: `db/alembic/versions/0017_setup_code.py`
- Modify: `services/api/bellasreef_api/security.py`
- Modify: `services/api/bellasreef_api/store.py`
- Test: `services/api/tests/test_setup_code.py` (create)

**Interfaces:**
- Consumes: `hash_refresh_token` construction (security.py:60 — SHA-256 hex) as the model for hashing; `hub_identity` singleton row (0012, store.py:111).
- Produces: `new_setup_code() -> str` (8 chars, Crockford base32 minus `0O1I`); `format_setup_code(code) -> str` (`XXXX-XXXX`); `normalize_setup_code(entry) -> str` (uppercase, dashes stripped); `hash_setup_code(code) -> str` (normalize then SHA-256 hex); `Store.set_setup_code_hash(hash) -> None` (rotate: overwrite); `Store.setup_state() -> tuple[str | None, datetime | None]` (hash, completed_at); `Store.complete_setup() -> None` (set `setup_completed_at = now()` if null).

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_setup_code.py
"""Setup-code primitives: alphabet, normalization, hashing, rotation."""

from bellasreef_api.security import (
    SETUP_ALPHABET,
    format_setup_code,
    hash_setup_code,
    new_setup_code,
    normalize_setup_code,
)


def test_alphabet_has_no_confusables() -> None:
    assert set("0O1I").isdisjoint(SETUP_ALPHABET)
    assert len(SETUP_ALPHABET) == 28  # Crockford base32 (no 0/O/1/I... spec: minus 0,O,1,I)


def test_code_shape() -> None:
    code = new_setup_code()
    assert len(code) == 8 and set(code) <= set(SETUP_ALPHABET)
    assert format_setup_code(code) == f"{code[:4]}-{code[4:]}"


def test_entry_is_case_and_dash_insensitive() -> None:
    assert normalize_setup_code("7kf2-9qmd") == "7KF29QMD"
    assert hash_setup_code("7kf2-9qmd") == hash_setup_code("7KF29QMD")


def test_codes_are_not_reused() -> None:
    assert new_setup_code() != new_setup_code()  # 40 bits; collision = bug in randomness
```

Note on the alphabet count: Crockford base32 already excludes I, L, O, U; the
spec says "Crockford base32 minus 0/O/1/I". Implement exactly the spec's
words: start from `ABCDEFGHJKMNPQRSTVWXYZ23456789` (Crockford's 32 minus
`0`, `1`) and assert the test against the real length of what you build —
fix the test's `28` to match, with a comment quoting the spec line.

- [ ] **Step 2: Run to verify failure**

Run: `cd services/api && uv run pytest tests/test_setup_code.py -v`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement security.py additions**

```python
#: Spec 2026-08-15: "Crockford base32 minus 0/O/1/I" - confusable-free.
SETUP_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ".translate(str.maketrans("", "", "OI"))


def new_setup_code() -> str:
    """8 chars, ~40 bits. Returned once; only the hash is stored."""
    return "".join(secrets.choice(SETUP_ALPHABET) for _ in range(8))


def format_setup_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_setup_code(entry: str) -> str:
    """Case-insensitive; the grouping dash is cosmetic and ignored (spec)."""
    return entry.replace("-", "").strip().upper()


def hash_setup_code(code: str) -> str:
    """Same construction as hash_refresh_token: SHA-256 hex over the
    normalized code. 'I forgot' is answered by rotating, not reprinting."""
    return hashlib.sha256(normalize_setup_code(code).encode()).hexdigest()
```

(Verify `hash_refresh_token`'s exact construction at security.py:60 and mirror
it — if it salts or prefixes, do the same.)

- [ ] **Step 4: Write the migration**

```python
# db/alembic/versions/0017_setup_code.py
"""Setup code + setup-completed marker on hub_identity.

Backfill: any hub that has ever paired a client is already set up and must
never re-enter setup mode (spec: a long-forgotten printed code must not
quietly become a key again).
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    op.add_column("hub_identity", sa.Column("setup_code_hash", sa.Text(), nullable=True))
    op.add_column(
        "hub_identity",
        sa.Column("setup_completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE hub_identity SET setup_completed_at = now() "
        "WHERE EXISTS (SELECT 1 FROM paired_clients)"
    )


def downgrade() -> None:
    op.drop_column("hub_identity", "setup_completed_at")
    op.drop_column("hub_identity", "setup_code_hash")
```

Match the file-header/revision-id style of `0016_pairing_code.py` exactly
(revision string format, type hints, docstring shape).

- [ ] **Step 5: Implement store methods**

Following `hub_identity` access idioms (store.py:111):

```python
async def setup_state(self) -> tuple[str | None, datetime | None]:
    async with self._engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT setup_code_hash, setup_completed_at FROM hub_identity"))
        ).first()
        return (row[0], row[1]) if row else (None, None)


async def set_setup_code_hash(self, code_hash: str) -> None:
    """Exactly one code valid at a time - minting rotates the old one out."""
    async with self._engine.begin() as conn:
        await conn.execute(text("UPDATE hub_identity SET setup_code_hash = :h"), {"h": code_hash})


async def complete_setup(self) -> None:
    """First successful pair, any method. Never unset."""
    async with self._engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE hub_identity SET setup_completed_at = now(), "
                " setup_code_hash = NULL WHERE setup_completed_at IS NULL"
            )
        )
```

Add store tests only if the file's peers test store methods against a real DB
fixture (they do in `test_auth_lifecycle.py` style — follow it; these run in
CI/loopback only).

- [ ] **Step 6: Run, check, commit**

```bash
cd services/api && uv run pytest tests/test_setup_code.py -v && cd ../..
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh
git add db services/api
git commit -m "feat(api): setup-code primitives and migration 0017

Confusable-free 8-char code, hashed at rest with the refresh-token
construction; hub_identity carries the hash and a never-unset
setup_completed_at, backfilled for any hub that has ever paired."
```

---

### Task 5: `/info` setup_mode + code-gated `POST /pair` + throttle

Spec Feature 1's wire surface.

**Files:**
- Modify: `services/api/bellasreef_api/app.py` (Info model + info handler at 750; PairRequest at 447; pair handler at 769; in-process throttle)
- Test: `services/api/tests/test_auth_lifecycle.py` (extend)

**Interfaces:**
- Consumes: Task 4's `setup_state`, `complete_setup`, `hash_setup_code`; the pair handler's existing grant path (whatever code today builds `PairGranted` — reuse it, do not duplicate token minting).
- Produces: `Info.setup_mode: bool`; `PairRequest.setup_code: str | None = None`; pair outcomes per spec: valid-in-setup → immediate `PairGranted`; invalid/missing-in-setup → 422 with a reason; non-null code outside setup → 422 ("never silently ignored"); 10 failed attempts/min globally → 429 with `Retry-After`. `client.paired` audit event gains `method: "setup_code" | "window" | "approval"` on ALL grant paths.

- [ ] **Step 1: Write the failing tests**

In `test_auth_lifecycle.py`, using its app/client fixtures:

```python
async def test_info_reports_setup_mode(fresh_client) -> None:
    info = (await fresh_client.get("/api/v1/info")).json()
    assert info["setup_mode"] is True  # nobody has ever paired


async def test_setup_code_pairs_immediately(fresh_client, minted_setup_code) -> None:
    r = await fresh_client.post(
        "/api/v1/pair", json={"client_name": "iPhone", "setup_code": minted_setup_code.lower()}
    )
    assert r.status_code == 200 and "refresh_token" in r.json()
    info = (await fresh_client.get("/api/v1/info")).json()
    assert info["setup_mode"] is False  # completed, never re-enters


async def test_wrong_code_is_rejected_not_pended(fresh_client, minted_setup_code) -> None:
    r = await fresh_client.post(
        "/api/v1/pair", json={"client_name": "iPhone", "setup_code": "XXXX-XXXX"}
    )
    assert r.status_code == 422


async def test_code_outside_setup_is_rejected(paired_client_env) -> None:
    r = await paired_client_env.post(
        "/api/v1/pair", json={"client_name": "Mallory", "setup_code": "7KF2-9QMD"}
    )
    assert r.status_code == 422  # never silently ignored


async def test_throttle_trips_at_ten_failures(fresh_client, minted_setup_code) -> None:
    for _ in range(10):
        await fresh_client.post(
            "/api/v1/pair", json={"client_name": "x", "setup_code": "WRONG-ONE"}
        )
    r = await fresh_client.post(
        "/api/v1/pair", json={"client_name": "x", "setup_code": "WRONG-ONE"}
    )
    assert r.status_code == 429 and "Retry-After" in r.headers
```

`minted_setup_code`: a fixture that calls `security.new_setup_code()`, stores
its hash via `store.set_setup_code_hash`, returns the plain code.

- [ ] **Step 2: Run to verify failure**

Run: `cd services/api && uv run pytest tests/test_auth_lifecycle.py -v -k setup`
Expected: FAIL.

- [ ] **Step 3: Implement**

`Info` model: add `setup_mode: bool`. Handler (750):

```python
        _, completed_at = await store.setup_state()
        return Info(..., setup_mode=completed_at is None)
```

`PairRequest` (447): add

```python
    #: Setup-mode bootstrap (spec 2026-08-15). Non-null outside setup mode is
    #: an error, never ignored.
    setup_code: str | None = Field(default=None, max_length=16)
```

Throttle — module-level, in-process by ruling ("a restart resetting the
throttle is acceptable at this threat model"):

```python
class _SetupThrottle:
    """10 failed code attempts per rolling minute, globally."""

    def __init__(self, limit: int = 10, window_s: float = 60.0) -> None:
        self._failures: deque[float] = deque()
        self._limit, self._window_s = limit, window_s

    def retry_after(self, now: float) -> int | None:
        while self._failures and now - self._failures[0] > self._window_s:
            self._failures.popleft()
        if len(self._failures) < self._limit:
            return None
        return int(self._window_s - (now - self._failures[0])) + 1

    def record_failure(self, now: float) -> None:
        self._failures.append(now)
```

Pair handler, at the top of the existing function body:

```python
code_hash, completed_at = await store.setup_state()
in_setup = completed_at is None
if body.setup_code is not None:
    if not in_setup:
        raise HTTPException(
            422,
            "This hub is already set up. Pair from "
            "an already-paired device, or run `bellasreef pair` on the hub.",
        )
    if (after := _setup_throttle.retry_after(time.monotonic())) is not None:
        raise HTTPException(
            429, "Too many attempts - wait a minute.", headers={"Retry-After": str(after)}
        )
    if code_hash is None or hash_setup_code(body.setup_code) != code_hash:
        _setup_throttle.record_failure(time.monotonic())
        await sink("pair.code_rejected", {"client_name": body.client_name})
        raise HTTPException(
            422,
            "That setup code is not right. It is on "
            "the deploy output on the hub; dashes and case do not matter.",
        )
    # valid: grant exactly as the open-window path does (reuse the
    # existing grant block), then:
    await store.complete_setup()
    await sink("client.paired", {"client_id": str(client_id), "method": "setup_code"})
    return granted
if in_setup and code_hash is not None:
    # a code has been minted; blind TOFU yields to it (spec: missing
    # code in setup mode is an explicit rejection, not a pending)
    raise HTTPException(422, "This hub is in setup. Enter the setup code from the deploy output.")
```

Read the existing handler carefully before splicing: reuse its grant block
(client insert + token mint) rather than duplicating it, keep the existing
TOFU behavior when NO code has ever been minted (a hub deployed without the
new deploy step must not brick first pair), and add `method: "window"` /
`"approval"` to the existing `client.paired`-equivalent sink calls on the
window and approval grant paths. Call `await store.complete_setup()` on EVERY
successful grant path — first pair by any method completes setup (spec).

- [ ] **Step 4: Run the suite**

Run: `cd services/api && uv run pytest -v`
Expected: PASS, including all pre-existing pairing tests (TOFU-without-code
path unchanged).

- [ ] **Step 5: Commit**

```bash
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh
git add services/api
git commit -m "feat(api): setup-mode pairing with a printed code

/info reports setup_mode; POST /pair takes an optional setup_code that
grants immediately in setup mode, rejects explicitly otherwise, and
throttles failures 10/min in process. First pair by any method
completes setup permanently. client.paired now records its method."
```

---

### Task 6: CLI `setup-code` subcommand + UX-6 pair copy

**Files:**
- Modify: `services/api/bellasreef_api/cli.py`
- Test: `services/api/tests/test_cli.py` (extend)

**Interfaces:**
- Consumes: Task 4 primitives; the CLI's existing subparser + DSN plumbing (cli.py:629, `_open_window` at 118 for output style).
- Produces: `bellasreef setup-code` — in setup mode, mints/rotates and prints the code block; after setup, prints the pointer text and exits 0. `bellasreef pair` output gains the UX-6 sentence.

- [ ] **Step 1: Write the failing tests**

Following `test_cli.py`'s existing invocation idiom (capsys + main(argv)):

```python
def test_setup_code_mints_in_setup_mode(fresh_db_env, capsys) -> None:
    rc = cli.main(["setup-code"])
    out = capsys.readouterr().out
    assert rc == 0
    assert re.search(r"\b[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}\b", out)
    assert "Open the Bella's Reef app" in out


def test_setup_code_rotates(fresh_db_env, capsys) -> None:
    cli.main(["setup-code"])
    first = capsys.readouterr().out
    cli.main(["setup-code"])
    second = capsys.readouterr().out
    assert first != second  # old code is invalid now; only the new hash stored


def test_setup_code_after_setup_is_informational(paired_db_env, capsys) -> None:
    rc = cli.main(["setup-code"])
    out = capsys.readouterr().out
    assert rc == 0 and "Setup is complete" in out


def test_pair_output_carries_the_ux6_sentence(fresh_db_env, capsys) -> None:
    # invoke the pair command the way the file's other pair tests do
    ...
    assert "cancel and pair again" in capsys.readouterr().out
```

(Adapt the character class in the regex to the final `SETUP_ALPHABET`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd services/api && uv run pytest tests/test_cli.py -v -k "setup_code or ux6"`
Expected: FAIL.

- [ ] **Step 3: Implement**

Subcommand registration beside the others (cli.py:634):

```python
    setup = sub.add_parser(
        "setup-code",
        help="Mint the first-pair setup code (setup mode only).",
    )
```

Handler, following `_backup_command`'s shape (sync wrapper over async helper):

```python
def _setup_code_command(args: Any, dsn: str) -> int:
    async def go() -> int:
        store = Store(dsn)  # match how other commands construct/open the store
        _, completed_at = await store.setup_state()
        if completed_at is not None:
            print(
                "Setup is complete. Pair new devices from the approver "
                "screen on an already-paired device, or open a window with "
                "`bellasreef pair` as the fire-escape."
            )
            return 0
        code = new_setup_code()
        await store.set_setup_code_hash(hash_setup_code(code))
        print(f"Setup code: {format_setup_code(code)}")
        print("Open the Bella's Reef app on this network and enter this code when asked.")
        return 0

    return asyncio.run(go())
```

UX-6: in `_open_window`'s success output (cli.py:118), append:

```python
print(
    "If a code is already showing in the app, cancel and pair again - "
    "requests created before this window stay pending."
)
```

- [ ] **Step 4: Run, check, commit**

```bash
cd services/api && uv run pytest tests/test_cli.py -v && cd ../..
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh
git add services/api
git commit -m "feat(cli): bellasreef setup-code, and the UX-6 pair sentence

Mints (rotating) the first-pair code in setup mode, points at the
approver screen after. Closes UX-6 from the 2026-08-14 iOS review."
```

---

### Task 7: contracts 3.7.0 — version bump, spec regen, contract tests

All additive surface from Tasks 2–5 ships as one semver-minor bump, per the
spec ("Pairing protocol (contract 3.7.0, semver-minor)").

**Files:**
- Modify: `contracts/python/bellasreef_contracts/__init__.py` (or wherever `SCHEMA_VERSION`/package version lives — `test_contracts_version.py` will point at it)
- Modify: the `DeviceAssignment`/audit payload models ONLY if contract tests demand (assignment payloads did not change shape)
- Modify: regenerated `openapi.json` via `python scripts/export-openapi.py`
- Test: `services/api/tests/test_contracts_version.py`, `contracts/python/tests/`

**Interfaces:**
- Consumes: everything Tasks 2–5 added to the OpenAPI surface.
- Produces: contracts version `3.7.0`; the committed OpenAPI artifact the iOS repo re-pins against.

- [ ] **Step 1: Bump the version** — read `test_contracts_version.py` to find every place the version is asserted (package version, `Info.contracts_version`, spec `info.version`), and move all of them to `3.7.0`.

- [ ] **Step 2: Regenerate the spec**

Run: `python scripts/export-openapi.py`
Confirm the diff shows: `setup_mode`, `setup_code`, `action`, `adopted`,
`readoptDevice`, `forgetDevice`, version `3.7.0` — and nothing else.

- [ ] **Step 3: Run everything**

Run: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh`
Expected: green, drift gate satisfied.

- [ ] **Step 4: Commit**

```bash
git add contracts services/api
git commit -m "feat(contracts): 3.7.0 - setup pairing, audit action, detached lifecycle

Additive: Info.setup_mode, PairRequest.setup_code, AuditEvent.action,
DeviceView.adopted, readoptDevice, forgetDevice, client.paired method."
```

---

### Task 8: deploy-pi.sh prints the setup code

Spec: "Every deploy in setup mode rotates the code; harmless before the first
pair, impossible after it."

**Files:**
- Modify: `scripts/deploy-pi.sh` (after the verify legs, as the final output)

- [ ] **Step 1: Implement**

At the end of the script's success path (after telemetry verification, before
the final success echo), following the script's existing ssh/compose-exec
idioms:

```bash
# New-owner bootstrap (spec 2026-08-15): if nobody has ever paired, every
# deploy rotates and prints the setup code as its final output.
setup_mode=$(ssh "$HOST" "curl -sf http://127.0.0.1:8000/api/v1/info" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["setup_mode"])')
if [[ "$setup_mode" == "True" ]]; then
    echo
    ssh "$HOST" "cd /home/david/bellasreef/deploy && docker compose exec -T api bellasreef setup-code"
fi
```

Match the variable names, quoting style, and error handling (`die`, `set -e`
posture) the script already uses; the API port/path must match the deploy's
own auth-leg check (the script already curls `/api/v1/info` — reuse that
mechanism rather than inventing a second one).

- [ ] **Step 2: Verify locally**

Run: `bash -n scripts/deploy-pi.sh` (syntax) and `shellcheck scripts/deploy-pi.sh`
if shellcheck is available. Actual behavior is proven on the first real
deploy (Task 10) and at the factory-reset acceptance.

- [ ] **Step 3: Commit**

```bash
git add scripts/deploy-pi.sh
git commit -m "feat(deploy): print the setup code when the hub is in setup mode"
```

---

### Task 9: scripts/factory-reset-pi.sh

Spec Feature 3, verbatim. This script is the sanctioned exception to "spine
data services are never recreated" — CLAUDE.md gets its one line.

**Files:**
- Create: `scripts/factory-reset-pi.sh`
- Modify: `CLAUDE.md` (one line in Deployment discipline pointing here)

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Factory-reset the hub: the 2026-08-14 manual wipe as one audited command.
#
# Destroys: bellasreef_postgres-data, bellasreef_vm-data, bellasreef_nats-data
# - all pairings, all devices, all telemetry history, the audit log.
# Keeps: /backups (a fresh pre-reset backup is mandatory and taken first),
# the git checkout, the images, /boot/firmware config.
#
# Sanctioned exception (spec 2026-08-15) to the deployment-discipline rule
# that spine data services are never recreated by a deploy.
#
# Acceptance is manual, on the hub; the dry run IS the 2026-08-14 transcript.
set -euo pipefail

HOST="${BELLASREEF_HOST:-reef}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/backups/bellasreef-pre-factory-${STAMP}.tar.gz"
COMPOSE="cd /home/david/bellasreef/deploy && docker compose"

die() { echo "factory-reset: $*" >&2; exit 1; }

# 1. Mandatory backup. No skip flag, by spec.
echo "Taking pre-reset backup to ${BACKUP} ..."
ssh "$HOST" "$COMPOSE exec -T api bellasreef backup --out '$BACKUP'" \
    || die "backup failed; aborting with nothing touched"

# 2. Informed consent, typed verbatim.
cat <<DOOM
About to DESTROY on ${HOST}:
  - docker volumes: bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data
  - every pairing, every device, the audit log, ALL telemetry history
Pre-reset backup: ${BACKUP} (on the hub)
DOOM
read -r -p "Type 'factory-reset' to proceed: " confirm
[[ "$confirm" == "factory-reset" ]] || die "not confirmed; nothing touched"

# 3. Stop the boot unit, then down (stopped containers still hold volume
#    references - measured 2026-08-14), then remove the volumes.
ssh "$HOST" "sudo systemctl stop bellasreef.service"
ssh "$HOST" "$COMPOSE down"
ssh "$HOST" "docker volume rm bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data"

# 4. Redeploy from zero. --no-verify is correct by construction: the
#    telemetry gate cannot pass on an empty registry (2026-08-12, 2026-08-14).
"$(dirname "$0")/deploy-pi.sh" --host "$HOST" --no-verify

# 5. Verify factory-fresh state, loudly.
echo "Verifying fresh state ..."
ssh "$HOST" "curl -sf http://127.0.0.1:8000/api/v1/info" | python3 -c '
import json, sys
info = json.load(sys.stdin)
assert info["paired_client_count"] == 0, info
assert info["setup_mode"] is True, info
print("  0 paired clients, setup mode open")'
ssh "$HOST" "$COMPOSE exec -T postgres psql -U bellasreef -tAc \
    'SELECT count(*) FROM devices; SELECT count(*) FROM audit_log;'"
ssh "$HOST" "docker logs bellasreef-hardware-io-1 2>&1 | grep -c 'capability announced'"

# 6. The setup code is the last thing on screen (deploy-pi.sh printed it;
#    reprint so it cannot scroll away), plus the one reminder that matters.
ssh "$HOST" "$COMPOSE exec -T api bellasreef setup-code"
echo "Reminder: adopt devices in the app before the deploy telemetry gate can pass again."
```

Adapt the psql invocation/user and the info-port mechanism to what
`deploy-pi.sh` itself uses (reuse its helpers if it has them; the script
above is the shape, deploy-pi.sh is the style guide). `chmod +x` the file.

- [ ] **Step 2: Add the CLAUDE.md line**

In the Deployment discipline bullet about spine data services, append:

```markdown
  The one sanctioned exception is `scripts/factory-reset-pi.sh` (spec
  2026-08-15): a deliberate, typed-confirmation wipe of the three data
  volumes with a mandatory pre-reset backup.
```

- [ ] **Step 3: Verify + commit**

Run: `bash -n scripts/factory-reset-pi.sh`

```bash
git add scripts/factory-reset-pi.sh CLAUDE.md
git commit -m "feat(scripts): factory-reset-pi.sh - the 2026-08-14 wipe as one command

Mandatory backup, typed confirmation, volume removal, redeploy from
zero with --no-verify (empty registry cannot pass the telemetry gate),
fresh-state verification, setup code last on screen."
```

---

### Task 10: PR, CI, deploy, wire-verify

**Files:** none (procedural gate)

- [ ] **Step 1:** Push the branch, open the PR (conventional title, plan+spec linked), wait for CI green.
- [ ] **Step 2:** Merge on David's review.
- [ ] **Step 3:** `./scripts/deploy-pi.sh` from main — the full gate: CI green → deploy → **telemetry verified on the wire** (registry is populated right now, so the gate can pass). The deploy also applies migration 0017.
- [ ] **Step 4:** On-hub smoke of the new surface, read-only: `curl 127.0.0.1:8000/api/v1/info` shows `setup_mode: false` (this hub has paired clients) and contracts `3.7.0`; `GET /api/v1/audit` rows show `action` and real categories for NEW events.
- [ ] **Step 5:** Hand off to the iOS plan (`../bellasreef-ios/docs/plans/2026-08-15-ux-fixes-and-onboarding.md`). The factory reset (Task 9's script) runs ONLY after the iOS work is done and David says go — he unpairs the sim first.

---

## Self-Review Notes

- Spec coverage: Feature 1 → Tasks 4–6, 8; Feature 3 → Task 9; contract bump → Task 7; testing section honored per-task (unit TDD, integration in CI/loopback only, factory-reset manual acceptance). Feature 2 lives in the iOS plan by design.
- The triage findings: hardware-io → Task 1; audit → Task 2; detached lifecycle → Task 3. History-tab and stale-row fixes are iOS-side (client bugs; server verified correct this morning).
- Type consistency: `readopt_device`/`forget_device` names match between store and endpoints; `ChannelHeldError` defined where used; `setup_state()` tuple shape used identically in Tasks 5 and 6.

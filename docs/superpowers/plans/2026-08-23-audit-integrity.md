# Audit Integrity (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The audit log records what happened, when it happened, to which device, in the category it was published under — instead of drain-time timestamps, NULL device_ids on every non-hardware-io row, and engine alert events remapped to "safety" attributed to "hardware-io".

**Architecture:** Three writer-side fixes in `AuditWriter._to_row` (2026-08-23 review, finding 10) plus one migration extending the `audit_log.category` CHECK to admit `'alert'`, plus actor stamps at the two publishers that omit them. The iOS rendering half (SF7/MF3) lives in the iOS plan, not here.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic (migration 0021), pytest.

**Spec:** 2026-08-23 `services/` code review finding 10. The audit trail's value was proven on 2026-08-23 (the 15%-vs-20% dispute); this plan is what makes that instrument trustworthy in the general case.

## Global Constraints

- Python 3.13+, `mypy --strict`, Ruff. Conventional commits, PR, CI green.
- `audit_log` is append-only by trigger — the migration touches only the CHECK constraint, never rows.
- Local gate: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh`. Deploy is part of done (migrations apply via the deploy script's one-off `docker compose run --rm api`).

## Context a fresh engineer needs

- `services/api/bellasreef_api/audit_writer.py::_to_row` (~176): `occurred_at=datetime.now(UTC)` (drain time); `device_id=event.get("actuator_id")` (only hardware-io's `command_refused` uses that key); `actor` defaults `"hardware-io"`; category token outside `_VALID_CATEGORIES` (63) is remapped to `"safety"` with `original_category` stashed — which is how every engine `alert.*` event is filed today, because the engine publishes on `subjects.audit("alert")` (engine app.py ~347).
- Payload timestamp keys in the wild: engine alerts carry `emitted_at`; hardware-io `command_refused` carries `observed_at`; API events carry neither (their drain lag is normally sub-second, so drain-time is an acceptable fallback there).
- Payload device keys in the wild: `actuator_id` (hardware-io command events), `device_id` (API device.* / thresholds.* events, engine alerts), `target` (override events), `channel_id` (schedule assign/unassign).
- Actor keys: API events set `actor`; engine `override.released` sets `"control-engine"`; engine alert events and hardware-io command events set none.
- `db/bellasreef_db/models.py::AuditLog` (~481): `category` CHECK `('command','config','auth','state','safety','calibration')`; `actor` is non-nullable `String(64)`.
- Migrations live in `db/` (Alembic; latest is 0020). Find the exact directory and template: `ls db/bellasreef_db/migrations/versions/ | tail -3` and copy the newest file's structure. The CHECK constraint's name is `category_valid` (models.py `__table_args__`).
- Writer tests: `services/api/tests/test_audit_writer.py` — has `_to_row` unit tests to extend.

---

### Task 1: migration 0021 — `'alert'` becomes a legal category

**Files:**
- Create: `db/bellasreef_db/migrations/versions/0021_audit_alert_category.py` (name/prefix per the existing convention — check `ls`)
- Modify: `db/bellasreef_db/models.py` (the CheckConstraint string)
- Modify: `services/api/bellasreef_api/audit_writer.py` (`_VALID_CATEGORIES` gains `"alert"`)

**Interfaces:**
- Produces: `audit_log.category` admits `'alert'`. Downgrade restores the old constraint (existing `'alert'` rows would block a downgrade — acceptable; note it in the migration docstring, matching how earlier migrations handle data-dependent downgrades — check 0016's pattern).

- [ ] **Step 1: Write the migration** (no failing-test step for DDL; the contract test is Task 2's writer test hitting a real/SQLite-faked constraint — follow however existing migration-adjacent tests assert constraints, e.g. the 0020 tests in `services/api/tests/test_chip_consumer.py`'s store setup)

```python
"""audit_log.category admits 'alert'.

The engine has published alert transitions on bellasreef.audit.alert since
threshold alerting shipped; the writer remapped them to 'safety' with
actor='hardware-io' because this CHECK predates the category. Post-incident
queries for category='alert' returned zero rows (2026-08-23 review, finding 10).
"""


def upgrade() -> None:
    op.drop_constraint("category_valid", "audit_log")
    op.create_check_constraint(
        "category_valid",
        "audit_log",
        "category IN ('command', 'config', 'auth', 'state', 'safety', 'calibration', 'alert')",
    )


def downgrade() -> None:
    # Refuses if 'alert' rows exist — append-only table, rows cannot be
    # rewritten, and silently re-filing them under 'safety' would repeat the
    # bug this migration fixes.
    op.drop_constraint("category_valid", "audit_log")
    op.create_check_constraint(
        "category_valid",
        "audit_log",
        "category IN ('command', 'config', 'auth', 'state', 'safety', 'calibration')",
    )
```

Adapt `revision`/`down_revision` headers to the real 0020 ids. Update models.py's CheckConstraint to match. Update `_VALID_CATEGORIES` to include `"alert"`.

- [ ] **Step 2: Run whatever migration test exists**

Run: `uv run pytest services/api/tests -k "migration or audit" -v` and `uv run alembic -c <the repo's alembic.ini path> heads` (verify single head).
Expected: PASS, one head = 0021.

- [ ] **Step 3: Commit**

```bash
git add db services/api/bellasreef_api/audit_writer.py
git commit -m "feat(db): audit_log admits category 'alert' (migration 0021)"
```

---

### Task 2: `_to_row` — event-time timestamps, real device ids, honest actor

**Files:**
- Modify: `services/api/bellasreef_api/audit_writer.py` (`_to_row` ~176)
- Test: `services/api/tests/test_audit_writer.py`

**Interfaces:**
- Produces: `_to_row` prefers, in order: `occurred_at` → `emitted_at` → `observed_at` (ISO-8601 strings; unparseable → drain time); `device_id` from `actuator_id` → `device_id` → `target` → `channel_id`; `actor` from the event with default `"unknown"` (the old `"hardware-io"` default attributed every unstamped row to a service that never published it).

- [ ] **Step 1: Write the failing tests**

```python
def test_row_keeps_the_event_time_not_the_drain_time():
    """Finding 10: after a writer stall, everything buffered in BR_AUDIT (24h
    retention) persisted misdated to drain time and misordered by
    ORDER BY occurred_at."""
    row = writer._to_row(
        "bellasreef.audit.alert", json.dumps({"emitted_at": "2026-08-23T01:02:03+00:00"}).encode()
    )
    assert row["occurred_at"] == datetime(2026, 8, 23, 1, 2, 3, tzinfo=UTC)


def test_row_resolves_device_id_across_publisher_dialects():
    for key in ("actuator_id", "device_id", "target", "channel_id"):
        row = writer._to_row("bellasreef.audit.command", json.dumps({key: "pca9685-0"}).encode())
        assert row["device_id"] == "pca9685-0", key


def test_alert_category_is_no_longer_remapped():
    row = writer._to_row("bellasreef.audit.alert", json.dumps({"device_id": "d"}).encode())
    assert row["category"] == "alert"
    assert "original_category" not in json.loads(row["event"])


def test_actor_default_is_unknown_not_hardware_io():
    row = writer._to_row("bellasreef.audit.config", b"{}")
    assert row["actor"] == "unknown"


def test_unparseable_timestamp_falls_back_to_drain_time():
    row = writer._to_row(
        "bellasreef.audit.command", json.dumps({"observed_at": "not-a-date"}).encode()
    )
    assert isinstance(row["occurred_at"], datetime)
```

Extend the existing `test_audit_writer.py` fixtures (it already constructs a writer without NATS for `_to_row` tests). Also update any existing test that asserted the old `"hardware-io"` default or the `safety` remap of `alert` — those assertions encode the bug.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/api/tests/test_audit_writer.py -v`
Expected: the new ones FAIL.

- [ ] **Step 3: Implement**

```python
_TIMESTAMP_KEYS: Final = ("occurred_at", "emitted_at", "observed_at")
_DEVICE_KEYS: Final = ("actuator_id", "device_id", "target", "channel_id")


def _event_time(event: dict[str, Any]) -> datetime:
    """The event's own clock, when it carries one. Drain time is a fallback,
    not a truth: after a writer stall, everything buffered in BR_AUDIT (24h)
    lands at once, and stamping arrival misorders the record the audit log
    exists to keep straight."""
    for key in _TIMESTAMP_KEYS:
        raw = event.get(key)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                continue
    return datetime.now(UTC)


def _event_device_id(event: dict[str, Any]) -> str | None:
    """Publishers name their subject differently: hardware-io says
    actuator_id, the API says device_id, overrides say target, schedule
    assignment says channel_id. The column answers 'which device' for all of
    them or per-device audit queries return nothing."""
    for key in _DEVICE_KEYS:
        raw = event.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None
```

In `_to_row`: use both helpers; `actor: str(event.get("actor", "unknown"))`. Timestamps parsed naive (no tzinfo) get `.replace(tzinfo=UTC)`? No — reject naive: `if parsed.tzinfo is None: continue` (a naive audit timestamp is a publisher bug; drain time is the safer record than a guessed zone). Write that decision as a one-line comment on the `continue`.

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/api/tests -v && uv run mypy services/api`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api
git commit -m "fix(api): audit rows keep event time, resolve device ids across publisher dialects, stop fabricating an actor"
```

---

### Task 3: publishers stamp their actor

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`_on_reading`'s alert audit dict ~348: add `"actor": "control-engine"`)
- Modify: `services/hardware_io/bellasreef_hardware_io/spine.py` (`CommandConsumer._audit` ~582: add `"actor": "hardware-io"` into the merged dict)
- Test: extend the nearest existing publish-shape tests (engine: wherever the alert-audit payload is asserted in `test_app.py`/`test_alerts.py`; hardware-io: the refusal-audit assertions in `test_spine.py`)

- [ ] **Step 1: Extend the payload-shape tests to require `actor`** (they will fail), e.g. assert `published_audit["actor"] == "control-engine"` on an alert transition and `"hardware-io"` on a `command_refused`.

- [ ] **Step 2: Run to verify they fail.** `uv run pytest services/control_engine/tests services/hardware_io/tests -k audit -v`

- [ ] **Step 3: Implement** — one key added per site:

engine `_on_reading` audit dict: `"actor": "control-engine",`
hardware-io `_audit`: `{"event": event_type, "actor": "hardware-io", **detail}` (spread last so an explicit actor in `detail` would win — none exists today, but the merge order is the honest one).

- [ ] **Step 4: Run both suites + types.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services
git commit -m "fix(audit): engine and hardware-io stamp their actor instead of inheriting the writer's guess"
```

---

### Task 4: PR, merge, deploy, verify on the hub

- [ ] **Step 1:** `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` green; push; PR `fix(audit): the log records event time, device, actor and category as published`. CI green → merge.
- [ ] **Step 2:** `scripts/deploy-pi.sh` (applies 0021 via the one-off migration run). Telemetry gate passes.
- [ ] **Step 3:** Verify on the hub: trigger one auditable event end-to-end — e.g. set-and-clear a threshold via the API, or create/release a hold via the API with a token — then `docker compose exec postgres psql -U bellasreef -d bellasreef -c "SELECT occurred_at, category, actor, device_id, event->>'event' FROM audit_log ORDER BY id DESC LIMIT 5"` and confirm: non-NULL device_id, actor naming the real publisher, category as published. Record the rows in the session report.

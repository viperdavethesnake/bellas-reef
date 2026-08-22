# Chip State PR 2 (API consumer + endpoint) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The API learns each chip's retained `ChipState` from `BR_CHIP` and serves it at `GET /api/v1/hardware`, so clients can render per-board facts (the Hardware leaf's data source).

**Architecture:** A `chip_state` table (one row per source+instance, migration 0020) upserted by a `ChipConsumer` mirroring `CapabilityConsumer` (ephemeral LAST_PER_SUBJECT subscription — see Ruling B); a read-only endpoint listing rows ordered by source then instance. Nothing to replay at startup: BR_CHIP is last-value, so the first delivery IS the retained state.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async + Alembic, FastAPI, nats-py, pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-chip-state-on-the-wire-design.md` (§API; Order step 2)

## Global Constraints

- Migration is **0020** (the spec's "0019" is superseded — schedules took it). `down_revision="0019"`; append `"0020"` to `db/bellasreef_db/revisions.py`'s tuple (test_revisions.py enforces).
- **Ruling B (binding, supersedes the spec's "durable `registry-chips`" parenthetical):** the consumer is an EPHEMERAL `js.subscribe(subjects.ALL_CHIPS, cb=..., config=ConsumerConfig(deliver_policy=DeliverPolicy.LAST_PER_SUBJECT))`, exactly `CapabilityConsumer`'s pattern (`services/api/bellasreef_api/registry.py:126-139` incl. the NotFoundError-wait retry loop). No durable: a durable would be single-consumer state on the shared broker — the contention class the environment boundary rule exists for. Flag stays in the PR body.
- Endpoint `GET /api/v1/hardware`, `operation_id="listHardware"`, `tags=["system"]` (match the tag `/api/v1/capabilities` uses — check and mirror), `Depends(current_client)`, additive OpenAPI only; regenerate `openapi.json` (spec-drift gate).
- `mypy --strict` clean; ruff clean; conventional commits; TDD; env-gated tests declared with `BELLASREEF_ALLOW_ENV_SKIPS=1`, loopback only.
- contracts stays 4.2.0 (this PR adds no wire types — `ChipStateView` is an API schema, not a contracts model).

---

### Task 1: `chip_state` table + migration 0020

**Files:**
- Modify: `db/bellasreef_db/models.py` (one ORM class, near `Capability` ~line 289 — mirror its docstring voice: chip state is tier-one hardware fact, replaced not merged)
- Create: `db/alembic/versions/0020_chip_state.py`
- Modify: `db/bellasreef_db/revisions.py` (append `"0020"`)
- Test: `db/tests/test_constraints.py` (append)

**Interfaces:**
- Produces ORM `ChipStateRow` (`__tablename__ = "chip_state"`): `id UUID pk` (use the file's `_uuid_pk()` helper), `source String(16) not null`, `instance String(64) not null`, `initialised Boolean not null`, `initialised_at DateTime(timezone=True) nullable`, `facts JSONB not null`, `announced_at DateTime(timezone=True) not null`, plus `UniqueConstraint("source", "instance")` (upsert target). Class name `ChipStateRow` to avoid colliding with the contracts message name in shared import contexts.

- [ ] **Step 1: Failing constraint test** — append to `db/tests/test_constraints.py` in its established style (module-level `_insert`-style helper + `run(scenario)` + `pytestmark = requires_postgres` per class; see `TestLightingSchedules` added 2026-08-22 for the exact shape): one test `test_one_chip_state_row_per_source_instance` — insert `("pca9685", "0x40@1", ...)` twice → second raises `IntegrityError` matching the named unique constraint (`uq_chip_state_source_instance` per the models' NAMING_CONVENTION).
- [ ] **Step 2: Run to verify** — env-skip locally is the declared RED; CI runs it.
- [ ] **Step 3: ORM class + migration** — named constraints (NAMING_CONVENTION parity — the schedules migration 0019 is the template: explicit `UniqueConstraint` with conventional name, `downgrade()` drops the table). Migration docstring cites the spec and the option-A ruling.
- [ ] **Step 4: `BELLASREEF_ALLOW_ENV_SKIPS=1 uv run pytest db/tests/ -v`** — green (revisions + offline render checked via gate).
- [ ] **Step 5: mypy + ruff, commit** — `feat(db): chip_state table (0020) — one row per hardware source instance`

---

### Task 2: store upsert + list

**Files:**
- Modify: `services/api/bellasreef_api/store.py` (two methods beside the capabilities block ~line 198)
- Test: `services/api/tests/test_chip_state_store.py` (new; `requires_postgres` harness like the store's other Postgres tests — find and mirror one)

**Interfaces:**
- Produces: `async def upsert_chip_state(self, *, source: str, instance: str, initialised: bool, initialised_at: datetime | None, facts: dict[str, Any], announced_at: datetime) -> None` — `INSERT ... ON CONFLICT (source, instance) DO UPDATE` (raw-SQL idiom like `replace_capabilities`); `async def list_chip_state(self) -> list[dict[str, Any]]` — all rows ordered by `source, instance`, keys matching the view model fields.

- [ ] **Step 1: Failing tests** — upsert twice (second updates `facts`/`announced_at`, still one row); list returns source-then-instance order (insert out of order, assert sorted); round-trip of a facts dict with str/int/float/bool values.
- [ ] **Step 2: verify (declared env-skip locally).**
- [ ] **Step 3: Implement** in `store.py`'s raw-SQL style.
- [ ] **Step 4: suite green; Step 5: mypy + ruff, commit** — `feat(api): chip-state store — upsert per (source, instance), ordered list`

---

### Task 3: `ChipConsumer` + lifespan wiring

**Files:**
- Modify: `services/api/bellasreef_api/registry.py` (new class after `CapabilityConsumer`)
- Modify: `services/api/bellasreef_api/app.py` (construct beside `capabilities = CapabilityConsumer(...)` ~line 775; start/close where that one is started/closed — find both sites)
- Test: `services/api/tests/` — mirror however `CapabilityConsumer` is tested (find its tests first; if they are NATS-env-gated, follow that; the message-handling method can be unit-tested directly with a fake msg object the way the capability one is, if it is)

**Interfaces:**
- Produces: `ChipConsumer(nats_url, store)` — `start()/close()/is_running`, `_on_message` validates `ChipState.model_validate_json(msg.data)` (ValidationError → warn + ignore, mirroring the capability consumer), then `store.upsert_chip_state(source=..., instance=..., ..., announced_at=now-UTC)`.

- [ ] **Step 1: Failing tests** — valid message → upsert called with the message's fields (fake store records calls); invalid JSON → warning, no upsert, no crash.
- [ ] **Step 2-5:** implement (copy `CapabilityConsumer`'s retry/subscribe shape verbatim, LAST_PER_SUBJECT, NO durable per Ruling B, subject `subjects.ALL_CHIPS`), suite, mypy + ruff, commit — `feat(api): ChipConsumer — BR_CHIP retained state into chip_state`

---

### Task 4: `GET /api/v1/hardware` + OpenAPI

**Files:**
- Modify: `services/api/bellasreef_api/app.py` (view model near the other view models; route near `/api/v1/capabilities` ~line 896)
- Modify: `openapi.json` (regenerate via `scripts/export-openapi.py`)
- Test: `services/api/tests/test_hardware_api.py` (new; same app-factory/auth harness as `/capabilities`' tests — find and mirror)

**Interfaces:**
- Produces: `class ChipStateView(BaseModel): source: str; instance: str; initialised: bool; initialised_at: datetime | None = None; facts: dict[str, str | int | float | bool]; announced_at: datetime` — route returns `list[ChipStateView]` from `store.list_chip_state()`, `operation_id="listHardware"`.

- [ ] **Step 1: Failing tests** — 401 unauthenticated; empty list on fresh store; rows come back ordered with facts intact (seed via the store); OpenAPI contains the operation_id.
- [ ] **Step 2-5:** implement, regenerate openapi.json, spec-drift green, suite, mypy + ruff, commit — `feat(api): GET /api/v1/hardware — per-chip state for the Hardware leaf`

---

## Self-review
- Spec §API coverage: consumer (T3, Ruling B), table+migration (T1, renumbered 0020), endpoint ordered by source+instance (T2 list + T4), nothing-to-replay note holds (LAST_PER_SUBJECT). iOS is PR 3.
- Placeholders: none; every test names its cases.
- Type consistency: `upsert_chip_state` kwargs = `_on_message`'s call = ORM columns; `list_chip_state` dict keys = `ChipStateView` fields.

# Lighting Schedules (backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move lighting schedules from `deploy/config/lighting.json` into Postgres as a named-schedule library the API owns and the engine re-reads every tick — create/edit/assign from clients, live within one tick, release-from-hold returns to the curve.

**Architecture:** One shared curve model in `bellasreef_contracts` (API validates writes, engine consumes reads). Two tables (`lighting_schedules`, `schedule_assignments`). A `ScheduleStore` in `bellasreef_db` beside `OverrideStore`. The engine's `_tick` reloads schedules exactly like it reloads overrides; `LightingScheduler` gains `set_profiles()` and nothing else — slew/deadband/hold machinery is untouched and does the converging.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async + Alembic, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-lighting-schedules-design.md`

## Global Constraints

- `mypy --strict` clean; ruff lint/format clean. Run both before every commit.
- Conventional commits; branch off `main`; no direct pushes to main.
- Contracts bump is **4.0.0 → 4.1.0** (additive only; `contracts/python/pyproject.toml`).
- Audit `category` must be one of the CHECK-constraint values (`db/bellasreef_db/models.py:451`); schedule events use `"config"`.
- The engine never clamps duty; the 8 % snap stays in the driver (`profiles.py` module docstring).
- Integration/DB tests: loopback only; Postgres-backed tests use the `requires_postgres` harness (`db/tests/helpers.py`), never the hub.
- Every new endpoint appears in OpenAPI with an `operation_id` (iOS client is generated).
- Delete, don't deprecate: `deploy/config/lighting.json`, `BELLASREEF_LIGHTING_PROFILES`, `load_profiles()`.

---

### Task 1: Shared curve model in contracts (4.1.0)

**Files:**
- Create: `contracts/python/bellasreef_contracts/schedules.py`
- Modify: `contracts/python/bellasreef_contracts/__init__.py` (exports)
- Modify: `contracts/python/pyproject.toml` (version `4.0.0` → `4.1.0`)
- Test: `contracts/python/tests/test_schedules.py`

**Interfaces:**
- Produces: `SchedulePoint(at: time, duty: float)` with `.seconds: int`;
  `validate_curve(points, zone, anchor, locale) -> None` (raises `ValueError`);
  `ScheduleDefinition(name: str, zone: str = "UTC", anchor: Anchor = "clock", locale: Locale | None, points: tuple[SchedulePoint, ...])`;
  re-exported `Anchor`, `Locale`, `OnMiss` literals (moved here from the engine).

- [ ] **Step 1: Write the failing tests**

```python
# contracts/python/tests/test_schedules.py
from datetime import time
import pytest
from pydantic import ValidationError
from bellasreef_contracts.schedules import ScheduleDefinition, SchedulePoint


def _points(*pairs: tuple[str, float]) -> list[dict[str, object]]:
    return [{"at": at, "duty": duty} for at, duty in pairs]


def test_valid_definition_round_trips() -> None:
    d = ScheduleDefinition.model_validate(
        {"name": "Bobs French Fries", "points": _points(("08:00", 0.0), ("13:00", 1.0))}
    )
    assert d.zone == "UTC"
    assert d.anchor == "clock"
    assert d.points[0].seconds == 8 * 3600


def test_fewer_than_two_points_rejected() -> None:
    with pytest.raises(ValidationError):
        ScheduleDefinition.model_validate({"name": "x", "points": _points(("08:00", 0.5))})


def test_unordered_and_duplicate_times_rejected() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        ScheduleDefinition.model_validate(
            {"name": "x", "points": _points(("13:00", 1.0), ("08:00", 0.0))}
        )
    with pytest.raises(ValidationError, match="same time"):
        ScheduleDefinition.model_validate(
            {"name": "x", "points": _points(("08:00", 0.0), ("08:00", 1.0))}
        )


def test_solar_anchor_rejected_until_v2() -> None:
    with pytest.raises(ValidationError, match="solar"):
        ScheduleDefinition.model_validate(
            {
                "name": "x",
                "anchor": "solar_natural",
                "locale": {"name": "Bora Bora", "lat": -16.5, "lon": -151.74},
                "points": _points(("08:00", 0.0), ("13:00", 1.0)),
            }
        )


def test_locale_on_clock_anchor_rejected() -> None:
    with pytest.raises(ValidationError, match="locale"):
        ScheduleDefinition.model_validate(
            {
                "name": "x",
                "locale": {"name": "Bora Bora", "lat": -16.5, "lon": -151.74},
                "points": _points(("08:00", 0.0), ("13:00", 1.0)),
            }
        )


def test_unknown_zone_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ScheduleDefinition.model_validate(
            {"name": "x", "zone": "Mars/Olympus", "points": _points(("08:00", 0.0), ("13:00", 1.0))}
        )


def test_microseconds_stripped() -> None:
    p = SchedulePoint(at=time(8, 0, 0, 123456), duty=0.5)
    assert p.at.microsecond == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest contracts/python/tests/test_schedules.py -v`
Expected: FAIL — `ModuleNotFoundError: bellasreef_contracts.schedules`

- [ ] **Step 3: Write the module**

Port the validation from `services/control_engine/bellasreef_control_engine/profiles.py` **verbatim where it exists** — this is a move, not a rewrite. `SchedulePoint` is `RampPoint` renamed (same `_no_microseconds` validator, same `seconds` property). `Anchor`, `Locale`, `OnMiss`, `_V2_ANCHORS` move here with their docstrings. Add:

```python
def validate_curve(
    points: Sequence[SchedulePoint], zone: str, anchor: Anchor, locale: Locale | None
) -> None:
    """The one curve-validity rule, shared by ScheduleDefinition (API writes)
    and the engine's ChannelProfile (reads). Raises ValueError."""
    if anchor in _V2_ANCHORS:
        raise ValueError(
            f"anchor={anchor!r} needs the solar model, which ships with lighting v2. "
            "Use anchor='clock'. The field exists now so v2 is an addition, not a migration."
        )
    if anchor == "clock" and locale is not None:
        raise ValueError(
            "locale is only meaningful with a solar anchor; a clock profile ignores it"
        )
    try:
        ZoneInfo(zone)
    except Exception as exc:
        raise ValueError(f"unknown timezone {zone!r}: {exc}") from exc
    seconds = [p.seconds for p in points]
    if seconds != sorted(seconds):
        raise ValueError("points must be in ascending time order")
    if len(set(seconds)) != len(seconds):
        raise ValueError("two points share the same time of day")


class ScheduleDefinition(_Frozen):  # same frozen/extra-forbid base as messages.py models
    name: str = Field(min_length=1, max_length=64)
    zone: str = "UTC"
    anchor: Anchor = "clock"
    locale: Locale | None = None
    points: tuple[SchedulePoint, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        validate_curve(self.points, self.zone, self.anchor, self.locale)
        return self
```

Export `SchedulePoint`, `ScheduleDefinition`, `validate_curve`, `Anchor`, `Locale`, `OnMiss` from `__init__.py`. Bump `contracts/python/pyproject.toml` to `4.1.0`. If `services/api/tests/test_contracts_version.py` pins the version, update it in the same commit.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest contracts/python/tests/ -v` — Expected: PASS (all, including existing).

- [ ] **Step 5: mypy + ruff, then commit**

```bash
uv run mypy --strict contracts/python && uv run ruff check . && uv run ruff format --check .
git add contracts/python && git commit -m "feat(contracts): shared lighting-curve model (4.1.0) — SchedulePoint, ScheduleDefinition, validate_curve"
```

---

### Task 2: Engine profiles delegate to the shared model

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/profiles.py`
- Test: `services/control_engine/tests/test_profiles.py` (add two tests; existing must pass unchanged)

**Interfaces:**
- Consumes: Task 1's `SchedulePoint`, `validate_curve`, `Anchor`, `Locale`, `OnMiss`.
- Produces: `RampPoint = SchedulePoint` (alias, so every existing engine test/import keeps working); `ChannelProfile.from_definition(channel_id: str, definition: ScheduleDefinition) -> ChannelProfile`.

- [ ] **Step 1: Write the failing tests**

```python
# append to services/control_engine/tests/test_profiles.py
from bellasreef_contracts.schedules import ScheduleDefinition, SchedulePoint
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint


def test_ramp_point_is_the_contracts_model() -> None:
    # One source of truth: the engine's point IS the wire point (spec §5).
    assert RampPoint is SchedulePoint


def test_from_definition_builds_equivalent_profile() -> None:
    d = ScheduleDefinition.model_validate(
        {
            "name": "This One",
            "zone": "America/Los_Angeles",
            "points": [{"at": "08:00", "duty": 0.0}, {"at": "13:00", "duty": 1.0}],
        }
    )
    p = ChannelProfile.from_definition("pi-pwm-0", d)
    assert p.channel_id == "pi-pwm-0"
    assert p.zone == "America/Los_Angeles"
    assert p.points == d.points
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest services/control_engine/tests/test_profiles.py -v`
Expected: FAIL — no `from_definition`; `RampPoint is not SchedulePoint`.

- [ ] **Step 3: Implement**

In `profiles.py`: delete the local `Anchor`/`OnMiss`/`_V2_ANCHORS`/`Locale`/`RampPoint` definitions and re-export them from `bellasreef_contracts.schedules` (`RampPoint = SchedulePoint`). Replace the body of `ChannelProfile._validate` with a call to `validate_curve(self.points, self.zone, self.anchor, self.locale)` (keep the docstrings that explain *why*; they move to the contracts module with the code). Add:

```python
@classmethod
def from_definition(cls, channel_id: str, definition: ScheduleDefinition) -> ChannelProfile:
    """An assignment row made concrete: this channel plays this schedule."""
    return cls(
        channel_id=channel_id,
        anchor=definition.anchor,
        zone=definition.zone,
        points=definition.points,
        locale=definition.locale,
    )
```

Update the module docstring: the model **is** a wire contract now (the app writes it) — the "engine configuration, not a wire contract" paragraph is superseded by spec 2026-08-19.

- [ ] **Step 4: Run the full engine suite**

Run: `uv run pytest services/control_engine/tests/ -v` — Expected: PASS, zero existing-test edits.

- [ ] **Step 5: mypy + ruff, commit**

```bash
uv run mypy --strict services/control_engine && uv run ruff check . && uv run ruff format --check .
git add services/control_engine contracts/python
git commit -m "refactor(control-engine): curve validation delegates to bellasreef-contracts; ChannelProfile.from_definition"
```

---

### Task 3: Tables + migration 0019

**Files:**
- Modify: `db/bellasreef_db/models.py` (two ORM classes)
- Create: `db/alembic/versions/0019_lighting_schedules.py`
- Modify: `db/bellasreef_db/revisions.py` (if it pins the head revision — check; `0018` → `0019`)
- Test: `db/tests/test_constraints.py` (append)

**Interfaces:**
- Produces: ORM `LightingSchedule` (`__tablename__ = "lighting_schedules"`: `id UUID pk`, `name String(64) unique not null`, `points JSONB not null`, `zone String(64) not null default 'UTC'`, `anchor String(16) not null default 'clock'`, `locale JSONB nullable`, `created_at/updated_at timestamptz`) and `ScheduleAssignment` (`__tablename__ = "schedule_assignments"`: `channel_id String(64) pk`, `schedule_id UUID FK lighting_schedules.id ondelete=RESTRICT not null`, `created_at`).

- [ ] **Step 1: Write the failing constraint tests**

```python
# append to db/tests/test_constraints.py — follow the file's existing fixture style
async def test_schedule_name_unique(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(_insert_schedule(name="This One"))
        with pytest.raises(IntegrityError):
            await conn.execute(_insert_schedule(name="This One"))


async def test_deleting_assigned_schedule_restricted(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        sid = (await conn.execute(_insert_schedule(name="That One"))).inserted_primary_key[0]
        await conn.execute(_insert_assignment(channel_id="pi-pwm-0", schedule_id=sid))
        with pytest.raises(IntegrityError):  # ON DELETE RESTRICT — the forgetDevice lesson
            await conn.execute(sa.delete(LightingSchedule).where(LightingSchedule.id == sid))


async def test_one_schedule_per_channel(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        a = (await conn.execute(_insert_schedule(name="A"))).inserted_primary_key[0]
        b = (await conn.execute(_insert_schedule(name="B"))).inserted_primary_key[0]
        await conn.execute(_insert_assignment(channel_id="pi-pwm-1", schedule_id=a))
        with pytest.raises(IntegrityError):  # channel_id is the PK; assign-replace is an upsert
            await conn.execute(_insert_assignment(channel_id="pi-pwm-1", schedule_id=b))
```

(Write `_insert_schedule`/`_insert_assignment` helpers in the test file with minimal valid `points` JSON — two points, ascending.)

- [ ] **Step 2: Run against the loopback test DB to verify failure**

Run: `BELLASREEF_TEST_DATABASE_URL=<loopback dev url> uv run pytest db/tests/test_constraints.py -v -k schedule`
Expected: FAIL — tables don't exist. (No container runtime → say so with `BELLASREEF_ALLOW_ENV_SKIPS=1` and let CI check it; never the hub.)

- [ ] **Step 3: ORM classes + migration**

Add the two classes to `models.py`, docstring pointing at the spec. Write `0019_lighting_schedules.py` (`revision="0019"`, `down_revision="0018"`) creating both tables:

```python
def upgrade() -> None:
    op.create_table(
        "lighting_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("points", postgresql.JSONB(), nullable=False),
        sa.Column("zone", sa.String(64), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("anchor", sa.String(16), nullable=False, server_default=sa.text("'clock'")),
        sa.Column("locale", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "schedule_assignments",
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.Uuid(),
            sa.ForeignKey("lighting_schedules.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
```

Migration docstring: named after David's ruling ("a set of points from midnight to midnight, for an individual PWM channel or group of PWM channels"), naming note re the AssignmentLedger collision (spec §Data model).

- [ ] **Step 4: Verify — drift test + constraint tests pass**

Run: `BELLASREEF_TEST_DATABASE_URL=<loopback url> uv run pytest db/tests/ -v`
Expected: PASS including `test_migration_drift.py` (ORM matches migration) and `test_revisions.py`.

- [ ] **Step 5: mypy + ruff, commit**

```bash
git add db && git commit -m "feat(db): lighting_schedules + schedule_assignments (0019); delete-assigned is RESTRICT"
```

---

### Task 4: ScheduleStore

**Files:**
- Create: `db/bellasreef_db/schedules.py`
- Modify: `db/bellasreef_db/__init__.py` (export)
- Test: `db/tests/test_schedule_store.py`

**Interfaces:**
- Consumes: Task 1 `ScheduleDefinition`; Task 3 ORM classes.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class StoredSchedule:
    id: UUID
    definition: ScheduleDefinition
    assigned_channels: tuple[str, ...]

class ScheduleInUseError(RuntimeError): ...

class ScheduleStore:
    def __init__(self, engine: AsyncEngine) -> None: ...
    async def create(self, definition: ScheduleDefinition) -> StoredSchedule           # ValueError on duplicate name
    async def update(self, schedule_id: UUID, definition: ScheduleDefinition) -> StoredSchedule  # KeyError if unknown
    async def delete(self, schedule_id: UUID) -> None                                   # ScheduleInUseError if assigned, KeyError if unknown
    async def get(self, schedule_id: UUID) -> StoredSchedule                            # KeyError if unknown
    async def list(self) -> list[StoredSchedule]
    async def assign(self, channel_id: str, schedule_id: UUID) -> None                  # upsert (assign replaces), KeyError if schedule unknown
    async def unassign(self, channel_id: str) -> bool                                   # False if nothing was assigned
    async def assigned_curves(self) -> dict[str, ScheduleDefinition]                    # channel_id -> definition; THE engine read, one join
```

- [ ] **Step 1: Write the failing tests** — Postgres-backed (`requires_postgres`), covering: create/get/list round-trip through `ScheduleDefinition` (points survive JSONB intact); duplicate name → `ValueError`; update replaces points; delete unknown → `KeyError`; delete assigned → `ScheduleInUseError`; assign upserts (second assign to same channel replaces, no error); unassign returns `True` then `False`; `assigned_curves()` returns exactly the assigned map; a row whose stored JSON no longer validates raises `ValueError` naming the schedule (loud, never silently skipped).

```python
# db/tests/test_schedule_store.py — representative core; write the rest in the same shape
async def test_assign_replaces_and_assigned_curves(engine: AsyncEngine) -> None:
    store = ScheduleStore(engine)
    a = await store.create(_definition("This One"))
    b = await store.create(_definition("That One"))
    await store.assign("pi-pwm-0", a.id)
    await store.assign("pi-pwm-0", b.id)  # replaces, per David's ruling — no 409 here
    curves = await store.assigned_curves()
    assert curves == {"pi-pwm-0": b.definition}


async def test_delete_assigned_raises(engine: AsyncEngine) -> None:
    store = ScheduleStore(engine)
    s = await store.create(_definition("Bobs French Fries"))
    await store.assign("pi-pwm-0", s.id)
    with pytest.raises(ScheduleInUseError):
        await store.delete(s.id)
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`.
- [ ] **Step 3: Implement** — model on `OverrideStore` (same engine-held pattern, `db/bellasreef_db/overrides.py:153`). Serialize with `definition.model_dump(mode="json")` for `points`/`locale`; deserialize with `ScheduleDefinition.model_validate`. Assign is `INSERT ... ON CONFLICT (channel_id) DO UPDATE`. Delete checks assignments explicitly first (clean `ScheduleInUseError`), FK RESTRICT as backstop. Module docstring: why it lives in the schema package (API and engine both consume; neither imports the other — same reasoning as `overrides.py:5-7`).
- [ ] **Step 4: Run** `db/tests/` — PASS.
- [ ] **Step 5: mypy + ruff, commit** — `feat(db): ScheduleStore — named curves, assign-replaces, delete-in-use refuses`

---

### Task 5: API endpoints + audit

**Files:**
- Modify: `services/api/bellasreef_api/app.py` (wire models near `OverrideRequest` ~line 543; routes near the overrides block ~line 1935; construct `ScheduleStore(engine)` beside `OverrideStore` ~line 693)
- Test: `services/api/tests/test_schedules_api.py`

**Interfaces:**
- Consumes: Task 4 `ScheduleStore` et al.; Task 1 `ScheduleDefinition`.
- Produces (wire, all additive):

```python
class ScheduleRequest(BaseModel):  # extra="forbid"
    name: str = Field(min_length=1, max_length=64)
    zone: str = "UTC"
    anchor: Anchor = "clock"
    locale: Locale | None = None
    points: list[SchedulePoint] = Field(min_length=2)


class ScheduleView(BaseModel):
    id: UUID
    name: str
    zone: str
    anchor: Anchor
    locale: Locale | None
    points: list[SchedulePoint]
    assigned_channels: list[str]


class ScheduleAssignRequest(BaseModel):  # extra="forbid"
    schedule_id: UUID
```

Routes (all `Depends(current_client)`, tags=["lighting"]):
| method+path | operation_id | notes |
|---|---|---|
| GET `/api/v1/lighting/schedules` | listSchedules | |
| POST `/api/v1/lighting/schedules` | createSchedule | 409 duplicate name |
| GET `/api/v1/lighting/schedules/{schedule_id}` | getSchedule | 404 |
| PUT `/api/v1/lighting/schedules/{schedule_id}` | updateSchedule | full replace; 404; 409 duplicate name |
| DELETE `/api/v1/lighting/schedules/{schedule_id}` | deleteSchedule | 409 if assigned; 404 |
| PUT `/api/v1/lighting/channels/{channel_id}/schedule` | assignSchedule | replaces; 404 unknown schedule; 409 if channel is registered `observe_only` (mirror `create_override`'s check; an **unknown** channel is allowed — schedule-before-adoption is legal, the engine holds) |
| DELETE `/api/v1/lighting/channels/{channel_id}/schedule` | unassignSchedule | 404 if nothing assigned |

Audit via the existing `sink(...)`, `category="config"`: `schedule.created` / `schedule.updated` / `schedule.deleted` (`schedule_id`, `name`, `actor`), `schedule.assigned` / `schedule.unassigned` (`channel_id`, `schedule_id`, `actor`). **Not clock-gated** — storing config needs no trusted clock (spec §Wire contracts).

- [ ] **Step 1: Write the failing tests** — follow the harness in `services/api/tests/test_device_binding.py` (same app factory + auth fixtures). Cover: CRUD round-trip; invalid curve → 422 (one point; duplicate times; solar anchor); duplicate name → 409 with the name in the detail; delete-assigned → 409; assign → replaces (second PUT wins, GET shows it); assign to observe_only device → 409; assign to unknown channel_id → 200 (pre-adoption legal); unassign → 200 then 404; each mutation writes exactly its audit row (assert via the audit list endpoint or sink capture, as `test_stream_and_overrides.py` does for overrides).
- [ ] **Step 2: Run to verify failure.** `uv run pytest services/api/tests/test_schedules_api.py -v` — FAIL (404s: routes missing).
- [ ] **Step 3: Implement** routes exactly in the `create_override` idiom (`app.py:1962` — try/except mapping `ValueError→409/422`, `KeyError→404`, `ScheduleInUseError→409`; audit after success, before return).
- [ ] **Step 4: Run the API suite.** `uv run pytest services/api/tests/ -v` — PASS.
- [ ] **Step 5: mypy + ruff, commit** — `feat(api): lighting schedule library — CRUD, assign/unassign, audited (contracts 4.1.0)`

---

### Task 6: `LightingScheduler.set_profiles`

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/scheduler.py`
- Test: `services/control_engine/tests/test_scheduler.py` (append)

**Interfaces:**
- Produces: `LightingScheduler.set_profiles(profiles: list[ChannelProfile]) -> None` — replaces the profile list; validates no duplicate channel_id (same check as `__init__`); **clears no history**.

- [ ] **Step 1: Write the failing tests**

```python
def test_set_profiles_curve_edit_converges_not_jumps() -> None:
    # slew configured; profile says 0.2, emit, then the curve is edited to 0.8:
    # the next intents ramp from 0.2 under the slew — a moved target, not a cold start.

def test_set_profiles_unassign_converges_to_dark() -> None:
    # channel emitted at 0.5, then set_profiles([]) removes it: due() surfaces it via the
    # existing synthetic path and walks it to SAFE_DUTY; once converged it goes quiet.

def test_set_profiles_preserves_hold_memory() -> None:
    # channel under a snap hold; set_profiles swaps in a new curve; hold still outranks,
    # and release after the swap snap-returns to the NEW curve's resting value.

def test_set_profiles_duplicate_channel_rejected() -> None:
    with pytest.raises(ValueError):
        scheduler.set_profiles([profile_a, profile_a])
```

Write them concretely in the file's existing style (fixed `datetime` clocks, `mark_emitted` after each `due`).

- [ ] **Step 2: Run to verify failure** — no `set_profiles`.
- [ ] **Step 3: Implement**

```python
def set_profiles(self, profiles: list[ChannelProfile]) -> None:
    """Swap the schedule set in place. Emission history is deliberately kept:
    a changed curve is a moved target the slew converges to; a removed
    assignment falls into the synthetic-channel path and converges to
    SAFE_DUTY; a held channel keeps its hold memory. Clearing history here
    would make every schedule edit a cold start — a visible pop for no reason.
    """
    ids = [p.channel_id for p in profiles]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate channel_id in profiles")
    self._profiles = profiles
```

- [ ] **Step 4: Run the scheduler suite** — PASS, existing tests untouched.
- [ ] **Step 5: mypy + ruff, commit** — `feat(control-engine): LightingScheduler.set_profiles — live schedule swap, history kept`

---

### Task 7: Engine reads schedules from Postgres; file path deleted

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`__init__` takes `schedule_store`; `_tick` reloads; `_amain` constructs `ScheduleStore(db)`; **delete** `load_profiles` and the `BELLASREEF_LIGHTING_PROFILES` read at `app.py:617-618`; metrics)
- Modify: `deploy/compose.yaml` (delete the `BELLASREEF_LIGHTING_PROFILES` line ~171 and the config mount if now unused)
- Delete: `deploy/config/lighting.json`
- Test: `services/control_engine/tests/test_app.py` (append)

**Interfaces:**
- Consumes: Task 4 `ScheduleStore.assigned_curves()`, Task 2 `ChannelProfile.from_definition`, Task 6 `set_profiles`.
- Produces: `ControlEngine(..., schedule_store: ScheduleStore | None = None)`; tick order `_expire_overrides → _reload_overrides → _reload_schedules → _sweep_silence → due`.

- [ ] **Step 1: Write the failing tests** — in `test_app.py`'s existing style (fake NATS, fake stores):

```python
async def test_schedule_edit_is_live_within_a_tick() -> None:
    # fake schedule store returns {"pi-pwm-0": curve_a}; tick; assert published duty matches
    # curve_a at the fake clock. Store now returns curve_b; next tick publishes toward curve_b.

async def test_schedule_store_error_keeps_last_good_set() -> None:
    # store raises on the second read; tick still emits from the first read's curves;
    # metric bellasreef_schedule_reload_errors_total incremented; exactly one warning log.

async def test_no_store_means_no_schedules_and_no_crash() -> None:
    # schedule_store=None (db-less dev): engine ticks, only override/synthetic paths run.

async def test_unassign_walks_channel_dark() -> None:
    # curves {"pi-pwm-0": a} then {}: published duty converges to 0.0 via slew, then quiet.
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement**

```python
async def _reload_schedules(self) -> None:
    """Same contract as _reload_overrides: Postgres is the source of truth and
    the tick re-reads it, so an edit the API made is live within one tick with
    no push channel to desync — the archive's schedules died of exactly that.
    On a read error, keep the last good set: a flapping database must not
    strip the tank's schedule."""
    if self.schedules is None:
        return
    try:
        curves = await self.schedules.assigned_curves()
    except Exception:
        self.metrics.schedule_reload_errors.inc()
        if not self._schedule_read_failing:  # one log per outage, not per tick
            self._schedule_read_failing = True
            log.warning("schedule reload failed; keeping last good set", exc_info=True)
        return
    self._schedule_read_failing = False
    if curves != self._last_curves:
        profiles = [ChannelProfile.from_definition(cid, d) for cid, d in sorted(curves.items())]
        self.scheduler.set_profiles(profiles)
        self._last_curves = curves
        self.metrics.lighting_schedules.set(len(profiles))
        log.info("schedules reloaded", extra={"channels": sorted(curves)})
```

Wire into `_tick` after `_reload_overrides()`. Constructor: `profiles` list parameter stays (tests use it) but `_amain` now passes `schedule_store=ScheduleStore(db) if db is not None else None` and no file profiles. Delete `load_profiles`, its import, the env read, `__all__` entry. Metrics: add `lighting_schedules` Gauge and `schedule_reload_errors` Counter to the metrics class (`app.py:73`).

- [ ] **Step 4: Run the engine suite** — PASS. Also `grep -rn "BELLASREEF_LIGHTING_PROFILES\|lighting.json" services deploy docs/contracts` → only historical docs hits.
- [ ] **Step 5: mypy + ruff, commit** — `feat(control-engine): schedules from Postgres per tick; lighting.json and BELLASREEF_LIGHTING_PROFILES deleted`

---

### Task 8: Journey test — the "it fires" proof

**Files:**
- Create: `services/control_engine/tests/test_schedule_journey.py`

**Interfaces:** consumes everything above; produces no new API.

- [ ] **Step 1: Write the journey** — one test, fixed clocks, fake NATS publisher, in-memory fake implementing `assigned_curves()` (the store's Postgres behavior is already proven in Task 4; this proves the *engine loop*): create curve (35 % at 08:00 → 100 % at 13:00) → assign → tick at 10:00 zone-local → published duty equals the interpolated curve value ±slew → edit curve → next tick moves toward the new value → place a ramp hold at 50 % → published duty is held → release → published duty returns toward curve-at-now (NOT SAFE_DUTY — the resting-state layer the hold spec deferred, now real) → unassign → converges to 0.0 → **repeat the first tick with identical state and assert an identical answer** (purity, the out-of-sync killer). Also assert the fire-time table: for three (clock, expected-duty) rows spanning the midnight wrap, `due()` emits the expected duty exactly.
- [ ] **Step 2: Run to verify it fails** where expected during development, then **passes** end-to-end.
- [ ] **Step 3: Full repo verification**

```bash
uv run mypy --strict . && uv run ruff check . && uv run pytest
```

Expected: all green (env-gated DB tests declared with `BELLASREEF_ALLOW_ENV_SKIPS=1` locally; CI runs them for real).

- [ ] **Step 4: Commit** — `test(control-engine): schedule journey — create→assign→fire→edit→hold→release-to-curve→unassign`

---

### Task 9: Contract prose + PR

**Files:**
- Modify: `docs/contracts/time-and-scheduling.md` (new §7 "Composition law", spec §Composition law verbatim: two layers, override wins, release returns to curve-at-now, future effects are pattern-overrides, future generated schedules stay behind `duty_at`)
- Modify: `CLAUDE.md` is **not** touched (deploy discipline unchanged)

- [ ] **Step 1: Write §7**, cross-referencing the spec and the hold-transition spec's closed deferral ("resting-state layer is the schedules round" — it is this round).
- [ ] **Step 2: Open the PR** (conventional title `feat(lighting): schedule library — Postgres-owned curves, live engine pickup`), body summarizing spec + the F-bar guarantees, ending with the standard generated-with footer. CI must be green.
- [ ] **Step 3: After merge: deploy.** `scripts/deploy-pi.sh` (applies 0019 via the one-off migration run), then on-hardware acceptance from the spec: assign a schedule to `pi-pwm-0`, watch the wire duty track the curve, meter one point at pin 32 (Stage-2 method). **Done = CI green → deployed → telemetry verified on the wire.**

---

## Self-review

- **Spec coverage:** ownership/flow (T7), tables (T3), store (T4), wire+audit+409s (T5), shared model (T1–2), `set_profiles` semantics (T6), F-bar journey + purity + fire-table (T8), composition-law prose + release-to-curve (T8, T9), file deletion (T7), accommodations (T1 carries anchor/locale; nothing else needed — effects/preview/lunar are later specs by design). iOS is deliberately a separate plan after this merges (generated client needs the real OpenAPI diff).
- **Placeholders:** none — every test step names its cases; Task 5 Step 1 and Task 6 Step 1 sketches are filled in against named existing harnesses (`test_device_binding.py`, `test_scheduler.py` styles).
- **Type consistency:** `ScheduleDefinition`/`SchedulePoint`/`validate_curve` (T1) consumed by name in T2/T4/T5/T7; `StoredSchedule.assigned_channels` used by `ScheduleView`; `assigned_curves() -> dict[str, ScheduleDefinition]` consumed verbatim in T7. `set_profiles(list[ChannelProfile])` matches T6/T7/T8.

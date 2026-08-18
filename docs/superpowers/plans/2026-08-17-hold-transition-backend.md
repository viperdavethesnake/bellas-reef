# Hold Transition (snap / ramp) — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An override carries `transition: "snap" | "ramp"`; the control engine jumps a snap hold in and out in one tick and keeps ramping a ramp hold at the global slew.

**Architecture:** One new column on `overrides` flows through `ActiveOverride` → the engine's `HeldTarget` → `LightingScheduler._emit_for`, which decides per target whether to apply `_limit`. The API grows the field on request, view and WebSocket frame (contracts 3.7.0 → 3.8.0, additive). Nothing on the NATS command contract changes; hardware-io is untouched.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async + Alembic (Postgres 17), FastAPI, pytest, `mypy --strict`, ruff. `uv` workspace at repo root.

**Spec:** `docs/superpowers/specs/2026-08-17-hold-transition-design.md`

## Global Constraints

- Python 3.13+, fully typed, `mypy --strict` clean; ruff lint + format clean (`uv run ruff check . && uv run ruff format --check .`).
- Contracts version bump is **3.7.0 → 3.8.0** — additive only. `openapi.json` and `stream-frames.schema.json` are committed artifacts regenerated with `uv run python scripts/export-openapi.py`; `deploy/avahi/bellasreef.service` `contracts=` TXT record must equal the package version (`scripts/check.sh` asserts both).
- Postgres-backed tests (`db/tests`, `services/control_engine/tests/test_overrides.py`, `services/api/tests/test_stream_and_overrides.py`) need `BELLASREEF_TEST_DATABASE_URL`; there is **no container runtime on the Mac**. Run them in CI. Locally run with `BELLASREEF_ALLOW_ENV_SKIPS=1` and say so in the task report — never point them at the hub (CLAUDE.md "Environment boundary").
- Every file keeps the SPDX header (`# SPDX-License-Identifier: AGPL-3.0-only` / `# SPDX-FileCopyrightText: 2026 Bella's Reef LLC`).
- Conventional commits; work on branch `feat/hold-transition` (already created, spec committed on it).
- Names used across tasks: `Transition = Literal["snap", "ramp"]` (defined in `db/bellasreef_db/overrides.py`, re-used by engine and API), `HeldTarget(duty, transition)` and `Intent.hold` in `scheduler.py`, intent reasons `hold` and `release`.

---

## File map

| File | Change |
|---|---|
| `db/alembic/versions/0018_override_transition.py` | **create** — column + CHECK, backfill by default |
| `db/bellasreef_db/revisions.py` | add `"0018"` |
| `db/bellasreef_db/models.py` | `Override.transition` mapped column + CHECK in `__table_args__` |
| `db/bellasreef_db/overrides.py` | `Transition` alias, `ActiveOverride.transition`, `OverrideStore.create(..., transition=)`, reads populate it |
| `services/control_engine/bellasreef_control_engine/scheduler.py` | `HeldTarget`, `Intent.hold`, `_last_hold`, `_emit_for` per-target slew decision |
| `services/control_engine/bellasreef_control_engine/app.py` | `_tick` builds `HeldTarget` |
| `services/control_engine/tests/test_scheduler.py`, `test_overrides.py`, `test_app.py` | update call sites; new behaviour tests |
| `services/api/bellasreef_api/app.py` | `OverrideRequest`/`OverrideView.transition`, pass to store, audit, view sites |
| `services/api/bellasreef_api/frames.py`, `stream.py` | `OverrideContext.transition` |
| `services/api/tests/test_stream_and_overrides.py` | request default / snap echo / 422 / audit / frame |
| `contracts/python/pyproject.toml`, `deploy/avahi/bellasreef.service`, `openapi.json`, `stream-frames.schema.json`, `uv.lock` | version bump + regenerated artifacts |

---

### Task 1: Storage — migration 0018, model, `ActiveOverride.transition`, store round-trip

**Files:**
- Create: `db/alembic/versions/0018_override_transition.py`
- Modify: `db/bellasreef_db/revisions.py:40` (append `"0018"`)
- Modify: `db/bellasreef_db/models.py:570-615` (`Override`)
- Modify: `db/bellasreef_db/overrides.py` (`ActiveOverride`, `OverrideStore.create/load_active/active_for/list_active`)
- Test: `db/tests/test_revisions.py` (existing, file-walk — runs without Postgres), `services/control_engine/tests/test_overrides.py` (Postgres)

**Interfaces:**
- Produces: `bellasreef_db.overrides.Transition = Literal["snap", "ramp"]`; `ActiveOverride.transition: Transition = "ramp"`; `OverrideStore.create(target, duty, duration_s, *, reason=None, transition: Transition = "ramp", now=None)`. All reads (`load_active`, `active_for`, `list_active`) populate `transition` from the row.

- [ ] **Step 1: Write the failing revision-list test expectation**

`db/tests/test_revisions.py::test_known_revisions_matches_the_migration_files` already asserts `KNOWN_REVISIONS == revisions on disk`. Creating the migration file first makes it fail until `revisions.py` is updated. Create the migration:

```python
# db/alembic/versions/0018_override_transition.py
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Overrides carry how the light moves to them: snap or ramp.

Spec 2026-08-17 (hold transition). A hold is the operator standing at the
tank asking for a level; the engine's global slew (1 %/s) is right for a
schedule and wrong for that. ``transition`` is the operator's choice, per
hold, and governs both ends — arrival and release/expiry alike.

A first-class column rather than a key in ``level``: ``level`` mirrors the
wire ``PwmLevel``, and transition is how the engine moves *between* levels,
not a property of one. hardware-io never sees it.

Backfill: every existing row is ``ramp`` — the behaviour it was placed
under.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "overrides",
        sa.Column(
            "transition",
            sa.String(4),
            nullable=False,
            server_default=sa.text("'ramp'"),
        ),
    )
    op.create_check_constraint(
        "override_transition_valid", "overrides", "transition IN ('snap', 'ramp')"
    )


def downgrade() -> None:
    op.drop_constraint("override_transition_valid", "overrides", type_="check")
    op.drop_column("overrides", "transition")
```

- [ ] **Step 2: Run the revision test to verify it fails**

Run: `cd /Users/david/visualstudio/bellasreef && uv run pytest db/tests/test_revisions.py -v`
Expected: FAIL — `KNOWN_REVISIONS has drifted from db/alembic/versions` (disk has 0018, list ends at 0017).

- [ ] **Step 3: Update the revision list and the model**

In `db/bellasreef_db/revisions.py`, append `"0018",` after `"0017",` inside `KNOWN_REVISIONS`.

In `db/bellasreef_db/models.py`, inside `class Override(Base)`, after the `reason` column add:

```python
    #: How the engine moves the light to this level and back: "snap" (one
    #: step) or "ramp" (the global slew). Spec 2026-08-17. Governs both ends
    #: of the hold — arrival and release/expiry alike.
    transition: Mapped[str] = mapped_column(String(4), server_default=text("'ramp'"))
```

and inside `__table_args__`, after the `release_reason_valid` CheckConstraint, add:

```
        CheckConstraint(
            "transition IN ('snap', 'ramp')",
            name="override_transition_valid",
        ),
```

(`text` and `String` are already imported in models.py — verify with `grep -n "^from sqlalchemy import" db/bellasreef_db/models.py`.)

- [ ] **Step 4: Run the revision test to verify it passes**

Run: `uv run pytest db/tests/test_revisions.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Write the failing store round-trip test (Postgres; runs in CI)**

Append to `services/control_engine/tests/test_overrides.py`, at module level (after the existing classes):

```python
class TestTransitionRoundTrip:
    """``transition`` is stored with the hold and comes back on every read
    path the engine and API use (spec 2026-08-17)."""

    def test_default_is_ramp_and_snap_round_trips(self) -> None:
        async def scenario() -> tuple[str, str, str, str]:
            engine = await fresh()
            store = OverrideStore(engine, clock_trusted=lambda: True)
            defaulted = await store.create("blue", 0.5, 1800, reason="photo")
            snapped = await store.create("white", 0.0, 900, reason="feed", transition="snap")
            active = {o.target: o for o in await store.list_active()}
            one = await store.active_for("white")
            woke = {o.target: o for o in await store.load_active()}
            assert one is not None
            await engine.dispose()
            return (
                defaulted.transition,
                active["white"].transition,
                one.transition,
                woke["blue"].transition,
            )

        assert run(scenario) == ("ramp", "snap", "snap", "ramp")

    def test_a_row_written_without_the_column_reads_back_as_ramp(self) -> None:
        """The backfill default, exercised the way an old row would be."""

        async def scenario() -> str:
            engine = await fresh()
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO overrides (id, target, level, created_at, expires_at) "
                        "VALUES (gen_random_uuid(), 'blue', CAST(:level AS JSONB), now(), "
                        "now() + interval '1 hour')"
                    ),
                    {"level": '{"kind": "pwm", "duty": 0.25}'},
                )
            found = await OverrideStore(engine).active_for("blue")
            await engine.dispose()
            assert found is not None
            return found.transition

        assert run(scenario) == "ramp"

    def test_an_unknown_transition_is_refused(self) -> None:
        async def scenario() -> None:
            engine = await fresh()
            store = OverrideStore(engine, clock_trusted=lambda: True)
            try:
                with pytest.raises(ValueError, match="transition"):
                    await store.create("blue", 0.5, 60, transition="fade")  # type: ignore[arg-type]
            finally:
                await engine.dispose()

        run(scenario)
```

- [ ] **Step 6: Run it to verify it fails (or is a declared skip locally)**

Run: `uv run pytest services/control_engine/tests/test_overrides.py -k Transition -v`
Expected locally: SKIP (`BELLASREEF_TEST_DATABASE_URL not set`) — with `BELLASREEF_ALLOW_ENV_SKIPS=1` exported if `conftest.py` demands it. In CI: FAIL with `TypeError: create() got an unexpected keyword argument 'transition'`.

- [ ] **Step 7: Implement `Transition`, `ActiveOverride.transition`, and the store**

In `db/bellasreef_db/overrides.py`:

Add near the other type aliases at the top (find `ReleaseReason`):

```python
#: How the engine moves the light to a held level and back. "snap" is one
#: step; "ramp" is the global slew. Governs both ends of the hold (spec
#: 2026-08-17). Kept beside ReleaseReason because the API and the engine
#: both import it from here — one source of truth per vocabulary.
Transition = Literal["snap", "ramp"]
TRANSITIONS: Final[frozenset[str]] = frozenset({"snap", "ramp"})
```

(Add `Literal`, `Final` to the `typing` import if missing.)

In `class ActiveOverride`, after `expires_at: datetime`:

```python
    #: "snap" or "ramp" — see :data:`Transition`. Defaults to "ramp" only so
    #: dataclass construction sites predating the field keep working; every
    #: store read sets it explicitly from the row.
    transition: Transition = "ramp"
```

`OverrideStore.create` — signature and body:

```python
    async def create(
        self,
        target: str,
        duty: float,
        duration_s: float,
        *,
        reason: str | None = None,
        transition: Transition = "ramp",
        now: datetime | None = None,
    ) -> ActiveOverride:
```

after the duty range check add:

```python
        if transition not in TRANSITIONS:
            raise ValueError(f"transition must be one of {sorted(TRANSITIONS)}, got {transition!r}")
```

change the INSERT to:

```python
            await conn.execute(
                text(
                    "INSERT INTO overrides (id, target, level, reason, created_at, expires_at, "
                    "transition) VALUES (:id, :target, CAST(:level AS JSONB), :reason, "
                    ":created, :expires, :transition)"
                ),
                {
                    "id": override_id,
                    "target": target,
                    "level": f'{{"kind": "pwm", "duty": {duty}}}',
                    "reason": reason,
                    "created": issued,
                    "expires": expires_at,
                    "transition": transition,
                },
            )

        active = ActiveOverride(
            id=override_id,
            target=target,
            duty=duty,
            expires_at=expires_at,
            transition=transition,
        )
```

Every SELECT (`load_active`, `active_for`, `list_active`) becomes `SELECT id, target, level, expires_at, transition FROM overrides ...` and each `ActiveOverride(...)` construction adds `transition=_transition(row[4])`. Add one module-level helper (keeps mypy honest about the Literal without a cast at every site):

```python
def _transition(raw: object) -> Transition:
    value = str(raw)
    if value == "snap":
        return "snap"
    if value == "ramp":
        return "ramp"
    # The CHECK constraint makes this unreachable; failing loudly beats
    # silently ramping a hold the operator asked to snap.
    raise ValueError(f"overrides.transition holds {value!r}, outside {sorted(TRANSITIONS)}")
```

- [ ] **Step 8: Run the gate for the touched packages**

Run:
```bash
uv run ruff check db services/control_engine && uv run ruff format --check db services/control_engine
uv run mypy --strict db services/control_engine
uv run pytest db/tests services/control_engine/tests/test_overrides.py -v
```
Expected: ruff/mypy clean; `db/tests/test_revisions.py` PASS; Postgres-backed tests SKIP locally (declared) — CI runs them.

- [ ] **Step 9: Commit**

```bash
git add db/alembic/versions/0018_override_transition.py db/bellasreef_db/revisions.py db/bellasreef_db/models.py db/bellasreef_db/overrides.py services/control_engine/tests/test_overrides.py
git commit -m "feat(db): overrides carry transition (snap|ramp) — migration 0018, ActiveOverride, store round-trip"
```

---

### Task 2: Engine — `HeldTarget`, per-target slew decision, snap release

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/scheduler.py`
- Modify: `services/control_engine/bellasreef_control_engine/app.py:448` (`_tick` builds the mapping)
- Modify: `services/control_engine/tests/test_scheduler.py:180-210`, `services/control_engine/tests/test_overrides.py:196,209`, `services/control_engine/tests/test_app.py:96` (call sites)
- Test: `services/control_engine/tests/test_scheduler.py` (new class `TestHoldTransition`)

**Interfaces:**
- Consumes: `bellasreef_db.overrides.Transition`, `ActiveOverride.transition` (Task 1).
- Produces: `bellasreef_control_engine.scheduler.HeldTarget(duty: float, transition: Transition)` (frozen dataclass); `LightingScheduler.due(now, overrides: Mapping[str, HeldTarget] | None = None)`; `Intent.hold: Transition | None = None`; reasons `"hold"` and `"release"`.

- [ ] **Step 1: Write the failing behaviour tests**

Add to `services/control_engine/tests/test_scheduler.py`. Update the import line to `from bellasreef_control_engine.scheduler import HeldTarget, LightingScheduler` and add the class:

```python
def snap(duty: float) -> HeldTarget:
    return HeldTarget(duty, "snap")


def ramp_hold(duty: float) -> HeldTarget:
    return HeldTarget(duty, "ramp")


class TestHoldTransition:
    """A target that comes from a hold moves the way that hold says
    (spec 2026-08-17). Slew 0.01/s with ticks 5 s apart: a ramp moves at most
    0.05 per tick, so anything larger in one intent is a snap."""

    def test_snap_hold_arrives_in_one_intent(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [intent] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        assert (intent.duty, intent.reason, intent.hold) == (1.0, "hold", "snap")

    def test_ramp_hold_still_slews(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [intent] = s.due(T0, {"pi-pwm-0": ramp_hold(1.0)})
        assert intent.duty < 1.0
        assert intent.hold == "ramp"

    def test_snap_release_jumps_to_resting_then_goes_quiet(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01, refresh_s=3600)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        [released] = s.due(T1, {})
        assert (released.duty, released.reason, released.hold) == (0.0, "release", None)
        s.mark_emitted(released, T1)
        assert s.due(T2, {}) == []

    def test_ramp_release_still_slews(self) -> None:
        # slew 0.1/s: T0 arrives at 0.0 (dt 0), T1 (+5 s) converges to 0.5,
        # then release 2 s later may move at most 0.2 -> 0.3, reason converge
        s = LightingScheduler([], max_duty_delta_per_s=0.1)
        [a] = s.due(T0, {"pi-pwm-0": ramp_hold(1.0)})
        s.mark_emitted(a, T0)
        [b] = s.due(T1, {"pi-pwm-0": ramp_hold(1.0)})
        assert b.duty == pytest.approx(0.5)
        s.mark_emitted(b, T1)
        [released] = s.due(at(9, 0, 7), {})
        assert (released.reason, released.hold) == ("converge", None)
        assert released.duty == pytest.approx(0.3)

    def test_supersede_ramp_with_snap_jumps(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [first] = s.due(T0, {"pi-pwm-0": ramp_hold(1.0)})
        s.mark_emitted(first, T0)
        [second] = s.due(T1, {"pi-pwm-0": snap(1.0)})
        assert (second.duty, second.reason, second.hold) == (1.0, "hold", "snap")

    def test_supersede_snap_with_ramp_ramps_and_forgets_snap(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01, refresh_s=3600)
        [first] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(first, T0)
        [second] = s.due(T1, {"pi-pwm-0": ramp_hold(0.0)})
        assert second.reason == "hold"  # arrival of the new hold is announced
        assert second.hold == "ramp"
        assert second.duty == pytest.approx(0.95)  # ramping down, not snapping
        s.mark_emitted(second, T1)
        # release now behaves as a ramp release: converge, not a jump
        [released] = s.due(T2, {})
        assert released.reason == "converge"
        assert released.duty == pytest.approx(0.90)

    def test_restart_rearm_honours_snap(self) -> None:
        # cold scheduler (engine restart) with a snap hold already owed
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [intent] = s.due(T0, {"pi-pwm-0": snap(0.7)})
        assert (intent.duty, intent.reason) == (0.7, "hold")

    def test_forget_clears_release_memory(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        s.forget("pi-pwm-0")
        # forgotten and unheld: nothing surfaces, and nothing snap-releases
        assert s.due(T1, {}) == []

    def test_reset_clears_release_memory(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        s.reset()
        assert s.due(T1, {}) == []

    def test_profiled_channel_snap_release_jumps_to_the_curve(self) -> None:
        s = LightingScheduler([ramp()], deadband=0.0, max_duty_delta_per_s=0.01)
        [held] = s.due(at(9), {"blue": snap(1.0)})
        s.mark_emitted(held, at(9))
        [released] = s.due(at(9, 0, 5), {})
        # 09:00 on the 06:00→12:00 ramp is 0.5; jump straight there
        assert released.reason == "release"
        assert released.duty == pytest.approx(0.5, abs=0.001)

    def test_snap_hold_at_target_refreshes_like_any_level(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01, refresh_s=10)
        [held] = s.due(T0, {"pi-pwm-0": snap(1.0)})
        s.mark_emitted(held, T0)
        assert s.due(T1, {"pi-pwm-0": snap(1.0)}) == []
        [again] = s.due(T2, {"pi-pwm-0": snap(1.0)})
        assert (again.reason, again.hold) == ("refresh", "snap")

    def test_due_stays_pure_while_held(self) -> None:
        s = LightingScheduler([], max_duty_delta_per_s=0.01)
        assert s.due(T0, {"pi-pwm-0": snap(1.0)}) == s.due(T0, {"pi-pwm-0": snap(1.0)})
```

Also update the existing call sites in the same file (`TestHeldUnprofiledChannels`) from `{"pi-pwm-0": 0.5}` to `{"pi-pwm-0": HeldTarget(0.5, "ramp")}` (and `0.3` likewise) — their behaviour under `ramp` is unchanged, so their assertions stay as they are. In `test_overrides.py` lines ~196 and ~209 change `{"blue": 0.0}` to `{"blue": HeldTarget(0.0, "ramp")}` and add `HeldTarget` to that file's scheduler import. In `test_app.py:96` (`_fresh`) add `transition=o.transition` to the copied `ActiveOverride`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest services/control_engine/tests/test_scheduler.py -v`
Expected: FAIL — `ImportError: cannot import name 'HeldTarget'`.

- [ ] **Step 3: Implement the scheduler change**

In `services/control_engine/bellasreef_control_engine/scheduler.py`:

Imports: add `from typing import Literal` if not present, and `from bellasreef_db.overrides import Transition`. Extend `__all__` with `"HeldTarget"`.

After `Intent`'s definition, replace `Intent` with:

```python
@dataclass(frozen=True, slots=True)
class Intent:
    """A decision, not yet a command.

    ``hold`` is the transition of the hold this intent was emitted under, or
    None when the channel was not held. :meth:`LightingScheduler.mark_emitted`
    reads it to keep the scheduler's per-channel hold memory exact without
    :meth:`LightingScheduler.due` mutating anything.
    """

    channel_id: str
    duty: float
    reason: str
    hold: Transition | None = None


@dataclass(frozen=True, slots=True)
class HeldTarget:
    """An operator hold as the scheduler sees it: a duty, and how to get there.

    ``transition`` is the operator's choice per hold (spec 2026-08-17). "snap"
    moves in one step regardless of the configured slew; "ramp" is today's
    slew-limited path. It governs both ends — arrival and release/expiry.
    """

    duty: float
    transition: Transition
```

In `__init__`, after `self._last_emitted_at`, add:

```python
        #: Transition of the last hold each channel was emitted under. Written
        #: and cleared only by mark_emitted (from Intent.hold), so due() stays
        #: pure. Consulted on the first tick a channel is no longer held: a
        #: snap hold releases in one step; anything else slews as before.
        self._last_hold: dict[str, Transition] = {}
```

`due` signature and body — replace `overrides: Mapping[str, float] | None` with `overrides: Mapping[str, HeldTarget] | None`, and rewrite the two loops and the synthetic set:

```python
        held = overrides or {}
        intents: list[Intent] = []
        for profile in self._profiles:
            # An override outranks the schedule while it is owed. How the
            # channel moves — snap or slew — is the hold's decision, on the
            # way in and on release alike.
            intent = self._emit_for(
                profile.channel_id, profile.duty_at(now), now, hold=held.get(profile.channel_id)
            )
            if intent is not None:
                intents.append(intent)

        # Held channels with no profile: an operator hold on an adopted
        # channel the config never mentions (every channel adopted through
        # the app, spec 2026-08-15). Semantics: a constant schedule of
        # SAFE_DUTY that the override outranks — release is just another
        # target change. Worth considering = currently held, still converging
        # from a hold that has already ended, or remembered as a snap hold
        # whose one-step release has not been emitted yet; once released and
        # converged it drops out on its own and stays quiet.
        profiled = {p.channel_id for p in self._profiles}
        synthetic = (
            set(held)
            | {cid for cid, duty in self._last_duty.items() if duty != SAFE_DUTY}
            | {cid for cid, t in self._last_hold.items() if t == "snap"}
        ) - profiled
        for channel_id in sorted(synthetic):
            intent = self._emit_for(channel_id, SAFE_DUTY, now, hold=held.get(channel_id))
            if intent is not None:
                intents.append(intent)

        return intents
```

Update the `due` docstring's `overrides` paragraph to: ``overrides`` maps channel to a :class:`HeldTarget` — the held duty and how to move to it. Passing them in rather than reaching for them keeps this function pure...

`_emit_for` — replace whole method:

```python
    def _emit_for(
        self, channel_id: str, resting: float, now: datetime, *, hold: HeldTarget | None
    ) -> Intent | None:
        """The shared per-channel emission decision.

        ``resting`` is what the channel should be at when nobody holds it (a
        profile's curve, or SAFE_DUTY for an unprofiled channel); ``hold`` is
        the operator's override if one is owed. Cold-start, convergence,
        deadband suppression and periodic refresh are target-agnostic, so
        this is the one place that logic lives; both loops in :meth:`due` call
        it rather than duplicating the block.

        Transition rule (spec 2026-08-17): a target that comes from a hold
        moves the way that hold says. A snap hold's intent *is* the target;
        a ramp hold goes through :meth:`_limit` like a schedule. A hold's
        arrival, or a change of transition while held, is always announced
        (reason ``hold``) even inside the deadband, so that after
        :meth:`mark_emitted` the hold memory is exact. On the first tick a
        channel is no longer held, a remembered snap hold releases to
        ``resting`` in one step (reason ``release``); anything else slews.
        """
        previous = self._last_duty.get(channel_id)
        cold = previous is None
        if previous is None:
            previous = SAFE_DUTY
        last_at = self._last_emitted_at.get(channel_id, now)
        dt_s = (now - last_at).total_seconds()
        remembered = self._last_hold.get(channel_id)

        if hold is None:
            if remembered == "snap":
                return Intent(channel_id, resting, "release")
            target = resting
            duty = self._limit(previous, target, dt_s)
            tag: Transition | None = None
        else:
            target = hold.duty
            duty = target if hold.transition == "snap" else self._limit(previous, target, dt_s)
            tag = hold.transition
            if remembered != hold.transition:
                # Arrival, or a supersede that changed the mode: announce it,
                # deadband or not, so mark_emitted records the new memory.
                return Intent(channel_id, duty, "hold", tag)
            if hold.transition == "snap" and duty != previous:
                # A snap hold re-held at a new duty: still a jump, still a hold.
                return Intent(channel_id, duty, "hold", tag)

        converging = duty != target
        if cold:
            return Intent(channel_id, duty, "initial", tag)
        if converging:
            # Mid-convergence. Emit even if this step is smaller than the
            # deadband, or a slow slew would stall short of the target and
            # sit there — the deadband exists to suppress noise, not
            # progress.
            return Intent(channel_id, duty, "converge", tag)
        if abs(duty - previous) >= self._deadband:
            return Intent(channel_id, duty, "ramp", tag)

        if channel_id not in self._last_emitted_at or dt_s >= self._refresh_s:
            return Intent(channel_id, duty, "refresh", tag)
        return None
```

`mark_emitted`, `reset`, `forget`:

```python
    def mark_emitted(self, intent: Intent, at: datetime) -> None:
        """Record that an intent was actually published.

        Also the only writer of the hold memory: an intent emitted under a
        hold records that hold's transition; an intent emitted while not held
        (a release, or ordinary convergence back to the resting target)
        clears it. A hold whose intent never went out is never remembered.
        """
        self._last_duty[intent.channel_id] = intent.duty
        self._last_emitted_at[intent.channel_id] = at
        if intent.hold is None:
            self._last_hold.pop(intent.channel_id, None)
        else:
            self._last_hold[intent.channel_id] = intent.hold
```

In `reset()` add `self._last_hold.clear()`; in `forget()` add `self._last_hold.pop(channel_id, None)` (and one sentence in its docstring: "The hold memory goes with it — a re-adopted channel must not snap-release on the strength of a hold that ended before its driver was rebuilt.").

Then in `services/control_engine/bellasreef_control_engine/app.py`, import `HeldTarget` from `.scheduler` (extend the existing import) and change `_tick`:

```python
        held = {t: HeldTarget(o.duty, o.transition) for t, o in self._held.items()}
```

- [ ] **Step 4: Run the engine suite and gate**

Run:
```bash
uv run pytest services/control_engine -v
uv run ruff check services/control_engine && uv run ruff format --check services/control_engine
uv run mypy --strict services/control_engine db
```
Expected: all `test_scheduler.py` PASS (including the pre-existing `TestHeldUnprofiledChannels` with the `HeldTarget(..., "ramp")` edits); Postgres tests SKIP locally (declared); ruff + mypy clean.


- [ ] **Step 5: Commit**

```bash
git add services/control_engine
git commit -m "feat(control-engine): holds carry snap|ramp — a snap hold jumps in and out; ramp keeps the global slew"
```

---

### Task 3: API — request/view/frame carry `transition`; contracts 3.8.0; regenerated artifacts

**Files:**
- Modify: `services/api/bellasreef_api/app.py:537-551` (`OverrideRequest`, `OverrideView`), `:1873`, `:1917-1940` (create endpoint: store call, audit, view)
- Modify: `services/api/bellasreef_api/frames.py:53-66` (`OverrideContext`), `services/api/bellasreef_api/stream.py:134` (build)
- Modify: `contracts/python/pyproject.toml:3` (`3.8.0`), `deploy/avahi/bellasreef.service:28` (`contracts=3.8.0`), `uv.lock`, `openapi.json`, `stream-frames.schema.json` (regenerated)
- Test: `services/api/tests/test_stream_and_overrides.py`

**Interfaces:**
- Consumes: `bellasreef_db.overrides.Transition`, `OverrideStore.create(..., transition=)`, `ActiveOverride.transition` (Task 1).
- Produces: `OverrideRequest.transition: Transition = "ramp"`, `OverrideView.transition: Transition`, `OverrideContext.transition: Transition`; audit `override.created` detail gains `"transition"`.

- [ ] **Step 1: Write the failing API tests (Postgres + NATS; run in CI)**

In `services/api/tests/test_stream_and_overrides.py`, inside `class TestOverrideEndpoints` add:

```
    def test_transition_defaults_to_ramp_and_snap_round_trips(self) -> None:
        async def scenario() -> dict[str, Any]:
            engine = await fresh_engine()
            await seed_devices(engine)
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(engine, app)
            out: dict[str, Any] = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                omitted = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={"target": "led-blue", "duty": 0.5, "duration_s": 60},
                )
                out["omitted"] = omitted.json()
                snapped = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={
                        "target": "led-blue",
                        "duty": 0.0,
                        "duration_s": 60,
                        "transition": "snap",
                    },
                )
                out["snapped"] = snapped.json()
                out["listed"] = (await c.get("/api/v1/overrides", headers=headers)).json()
                bad = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={
                        "target": "led-blue",
                        "duty": 0.0,
                        "duration_s": 60,
                        "transition": "fade",
                    },
                )
                out["bad_code"] = bad.status_code
            await engine.dispose()
            out["created_details"] = [
                d for e, d, _ in audit.records if e == "override.created"
            ]
            return out

        out = run(scenario)
        assert out["omitted"]["transition"] == "ramp"
        assert out["snapped"]["transition"] == "snap"
        assert [o["transition"] for o in out["listed"]] == ["snap"]  # superseded the ramp one
        assert out["bad_code"] == 422
        assert [d["transition"] for d in out["created_details"]] == ["ramp", "snap"]
```

In `test_an_authenticated_socket_gets_ready_then_live_frames`, change the store call to `await OverrideStore(engine).create("led-blue", 0.0, 1800, reason="feed", transition="snap")` and add after the `expires_in_s` assertion:

```python
            assert frame["override"]["transition"] == "snap"
```

- [ ] **Step 2: Run to verify failure (locally: declared skip; CI: FAIL)**

Run: `uv run pytest services/api/tests/test_stream_and_overrides.py -k "transition or live_frames" -v`
Expected: SKIP locally (`BELLASREEF_TEST_DATABASE_URL not set`); in CI FAIL — `KeyError: 'transition'`.

- [ ] **Step 3: Implement the API change**

`services/api/bellasreef_api/app.py` — import `Transition` from `bellasreef_db.overrides` (there is already an import from that module for `ClockUntrustedError`/`OverrideStore`; extend it). Then:

```python
class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=64)
    duty: float = Field(ge=0.0, le=1.0)
    duration_s: float = Field(gt=0.0, le=86400.0)
    reason: str | None = Field(default=None, max_length=256)
    #: How the light moves to this level and back: "snap" (one step) or
    #: "ramp" (the engine's global slew). Governs both ends of the hold —
    #: arrival and release/expiry (spec 2026-08-17). Defaults to "ramp",
    #: which is what every client before 3.8.0 got.
    transition: Transition = "ramp"


class OverrideView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    target: str
    duty: float
    expires_at: datetime
    expires_in_s: float
    transition: Transition
```

`list_overrides` (≈line 1873): add `transition=o.transition,` to the `OverrideView(...)`.

`create_override` (≈line 1917): `placed = await overrides.create(body.target, body.duty, body.duration_s, reason=body.reason, transition=body.transition)`; audit detail gains `"transition": placed.transition,`; the returned `OverrideView(...)` gains `transition=placed.transition,`.

`services/api/bellasreef_api/frames.py` — import `Transition` from `bellasreef_db.overrides` and add to `OverrideContext`:

```python
    #: "snap" or "ramp" — how the engine will move the light when this hold
    #: ends, as much as how it arrived. Shown on the active-hold row so what
    #: happens at expiry is legible (spec 2026-08-17).
    transition: Transition
```

`services/api/bellasreef_api/stream.py:134` — add `transition=active.transition,` to the `OverrideContext(...)`.

- [ ] **Step 4: Bump contracts to 3.8.0 and regenerate the artifacts**

```bash
sed -i '' 's/^version = "3.7.0"/version = "3.8.0"/' contracts/python/pyproject.toml
sed -i '' 's|<txt-record>contracts=3.7.0</txt-record>|<txt-record>contracts=3.8.0</txt-record>|' deploy/avahi/bellasreef.service
uv lock
uv run python scripts/export-openapi.py
git diff --stat
```
Expected: `uv.lock` changes the `bellasreef-contracts` version line only; `openapi.json` diff shows `info.version` 3.8.0 and `transition` under `OverrideRequest` (with `default: ramp`) and `OverrideView` (required); `stream-frames.schema.json` shows `transition` under `OverrideContext` (required). If the diff shows anything else, stop and report — the spec says the diff is exactly this.

Check `tests/test_install_hub.py` for hardcoded `"contracts_version":"3.7.0"` stubs (lines ≈536, 2163, 2324): read the surrounding assertions; if a test compares the stub to the installed version, update the stub to `3.8.0`; if it is opaque fake output, leave it.

- [ ] **Step 5: Run the gate**

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict contracts/python db services
uv run pytest services/api services/control_engine db tests -q
scripts/check.sh   # if it runs locally; note the deploy/.env trap in memory — report, don't work around
```
Expected: ruff/mypy clean; `services/api/tests/test_contracts_version.py` PASS (3.8.0 installed = advertised); OpenAPI/frames drift check clean; avahi record check clean; Postgres/NATS tests SKIP locally (declared).

- [ ] **Step 6: Commit**

```bash
git add services/api contracts/python/pyproject.toml deploy/avahi/bellasreef.service uv.lock openapi.json stream-frames.schema.json tests
git commit -m "feat(api): overrides carry transition (snap|ramp) on request, view and stream frames — contracts 3.8.0"
```

---

### Task 4: PR, CI, deploy, bench card

**Files:** none new. Uses `scripts/deploy-pi.sh`.

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feat/hold-transition
gh pr create --title "feat: hold transition — snap or ramp, per hold (contracts 3.8.0)" --body "$(cat <<'EOF'
Spec: docs/superpowers/specs/2026-08-17-hold-transition-design.md
Plan: docs/superpowers/plans/2026-08-17-hold-transition-backend.md

- db: migration 0018 `overrides.transition` (snap|ramp, default ramp), `ActiveOverride.transition`, store round-trip
- control-engine: `HeldTarget`; a snap hold jumps in and out in one tick, a ramp hold keeps the global 1 %/s slew; release memory kept exact via `Intent.hold`/`mark_emitted`
- api: `transition` on `OverrideRequest` (default ramp), `OverrideView`, `OverrideContext`; audit carries it; contracts 3.7.0 → 3.8.0 (additive); artifacts regenerated

No change to the NATS command contract; hardware-io untouched.

Bench proof after deploy (David's meter, Light 0 pin 32 and Light 1 LED0): snap Hold 0→100 % reaches 3.308 V within ~1 s; Release drops to 0 V within ~1 s; ramp Hold still takes ~100 s; snap Hold at 5 % reads 0 V.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

If the pre-push hook refuses because of the local `deploy/.env` trap, report it — David pushes with `! git push --no-verify`.

- [ ] **Step 2: Watch CI**

Run: `gh pr checks --watch`
Expected: lint, types, tests (incl. the Postgres/NATS suites), multi-arch build all green. On red: read the failing job log, fix on the branch, push again — do not merge red.

- [ ] **Step 3: Merge and deploy**

```bash
gh pr merge --squash --delete-branch
git checkout main && git pull
scripts/deploy-pi.sh
```
Expected: the script refuses a dirty/unpushed tree, resets the Pi, pulls images by digest, applies migration 0018 via `docker compose run --rm api`, recreates the three app services and **verifies telemetry on the wire**. Confirm `ssh <pi> 'docker compose -f /home/david/bellasreef/deploy/compose.yaml exec -T postgres psql -U ... -c "\d overrides"'` shows `transition` (or read it via `docker compose logs api` migration output).

- [ ] **Step 4: Bench card for David**

Hand over, and stop — David drives the app and reads the meter:

| Step | App | Meter (Light 0, pin 32) | Expect |
|---|---|---|---|
| 1 | Hold 100 % — the app has no toggle yet, so it sends `ramp` (default) | ~100 s to 3.308 V | ramp intact |
| 2 | `curl -X POST .../api/v1/overrides -d '{"target":"pi-pwm-0","duty":1.0,"duration_s":600,"transition":"snap"}'` (David runs it via `!` with a token) | 3.308 V within ~1 s | snap arrival |
| 3 | Release from the app | 0 V within ~1 s | snap release |
| 4 | Same as 2 with `"duty":0.05` | 0 V | driver snap-to-0 still applies |
| 5 | Steps 2–3 on `pca9685-0` (LED0) | same numbers | cross-silicon |

Engine log check for each row: `docker logs bellasreef-control-engine-1 | grep "lighting:hold\|lighting:release"`.

Until the iOS PR lands, the app sends `ramp`; the curl rows are the snap proof.

---

## Self-review

- **Spec coverage:** Contract (Task 3), Storage (Task 1), Engine rule + release + supersede + restart + forget/reset (Task 2), <8 % untouched (no task — nothing to change, Task 4 row 4 proves it), logging reasons (Task 2), client (separate repo — out of this plan by the spec), Testing (Tasks 1–3 unit/integration; Task 4 bench), deploy gate (Task 4).
- **Placeholders:** none; every step has code or an exact command.
- **Type consistency:** `Transition` from `bellasreef_db.overrides` in all three tasks; `HeldTarget(duty, transition)` positional in tests and `_tick`; `Intent.hold`; reasons `hold`/`release`; store kwarg `transition=`; API/frames field `transition`.

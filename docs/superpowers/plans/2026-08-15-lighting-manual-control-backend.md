# Lighting Manual Control — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An override held for an adopted channel with no configured profile
actually commands the light — held duty while owed, slewing back to safe 0 on
expiry or release — with no wire-contract change.

**Architecture:** `LightingScheduler.due()` iterates the union of configured
profiles and held-but-unprofiled channels, the latter behaving as a synthetic
constant-`SAFE_DUTY` schedule. Every existing mechanism (override-outranks-
schedule, slew limiting, deadband vs convergence, cold start, adoption
suppression in the engine app) applies unchanged; there is no second path.

**Tech Stack:** Python 3.13, pytest; contracts untouched at 3.7.0.

**Spec:** `docs/superpowers/specs/2026-08-15-lighting-manual-control-design.md`
(Feature 1). Committed on this branch.

## Global Constraints

- `mypy --strict` clean, Ruff clean; `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` before every commit (no container runtime on this Mac; env-skips declared).
- NO OpenAPI/contract change; if check.sh's drift gate reports one, something is wrong — stop.
- Profiled-channel behavior byte-identical: the existing scheduler tests must pass unmodified (changing an existing assertion is a red flag to justify in the report, not a routine edit).
- Conventional commits + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer. Branch: feat/lighting-manual-control (exists).
- Integration tests never touch the hub.

---

### Task 1: scheduler emits for held, unprofiled channels

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/scheduler.py` (`due()` at ~:85-138; module docstring if it enumerates emission sources)
- Test: `services/control_engine/tests/test_scheduler.py` (extend)

**Interfaces:**
- Consumes: `Intent`, `SAFE_DUTY` (=0.0), `self._profiles` (list of `ChannelProfile`, `.channel_id`, `.duty_at(now)`), `self._last_duty` / `self._last_emitted_at` / `self._limit(previous, target, dt_s)` — all existing (scheduler.py:31, :45, :75-84, :140+).
- Produces: `due(now, overrides)` also yields intents for channels present in `overrides` but absent from `self._profiles`, computed with `duty_at ≡ SAFE_DUTY` semantics. After such a channel's override ends AND it has converged to `SAFE_DUTY`, it emits nothing further. `channel_ids` property stays profiles-only (it feeds startup logging of *configured* channels — verify its callers before deciding otherwise; if a caller needs held channels too, that caller is out of scope here).

- [ ] **Step 1: Write the failing tests**

Follow test_scheduler.py's existing fixture idioms (fixed timezone-aware
clocks, mark_emitted between due() calls). Add, in the file's style:

```python
def test_held_unprofiled_channel_is_emitted() -> None:
    sched = LightingScheduler([], max_duty_delta_per_s=None)  # no profiles
    intents = sched.due(T0, {"pi-pwm-0": 0.5})
    assert [(i.channel_id, i.duty) for i in intents] == [("pi-pwm-0", 0.5)]


def test_held_unprofiled_channel_slews_from_safe_start() -> None:
    # with a slew rate configured, the first emission climbs from SAFE_DUTY
    # toward the held duty rather than popping to it
    sched = LightingScheduler([], max_duty_delta_per_s=0.1)
    first = sched.due(T0, {"pi-pwm-0": 0.5})
    assert first and first[0].duty < 0.5  # converging, not popped


def test_release_slews_back_to_safe_and_goes_quiet() -> None:
    sched = LightingScheduler([], max_duty_delta_per_s=None)
    held = sched.due(T0, {"pi-pwm-0": 0.5})
    sched.mark_emitted(held[0], T0)
    # override gone: target falls to SAFE_DUTY
    released = sched.due(T1, {})
    assert [(i.channel_id, i.duty) for i in released] == [("pi-pwm-0", 0.0)]
    sched.mark_emitted(released[0], T1)
    # converged at 0: nothing further
    assert sched.due(T2, {}) == []


def test_profiled_channels_unaffected_by_held_strangers() -> None:
    # a profile plus a held stranger: both emit, profile from its own curve
    sched = LightingScheduler([PROFILE_LED_BLUE], max_duty_delta_per_s=None)
    intents = sched.due(T0, {"pi-pwm-0": 0.3})
    ids = {i.channel_id for i in intents}
    assert ids == {"led-blue", "pi-pwm-0"}
```

Adapt constant names (T0/T1/T2, PROFILE_LED_BLUE) to what the file already
defines or add minimal ones in its style. The exact assertion shapes above are
the substance; the reason strings on intents should follow the file's existing
expectations (read what `reason` values existing tests pin — "initial" /
"converge" / the steady-state reason — and assert consistently: the FIRST
emission for a cold synthetic channel is whatever the cold branch emits today).

- [ ] **Step 2: Run to verify failure**

Run: `cd services/control_engine && uv run pytest tests/test_scheduler.py -v -k "held or release or strangers"`
Expected: FAIL — no intents for unprofiled channels.

- [ ] **Step 3: Implement**

In `due()`, after the existing profile loop, iterate synthetic channels:

```python
        # Held channels with no profile: an operator hold on an adopted
        # channel the config never mentions (every channel adopted through
        # the app, spec 2026-08-15). Semantics: a constant schedule of
        # SAFE_DUTY that the override outranks — release is just another
        # target change, and the same slew/deadband/convergence machinery
        # applies. A synthetic channel that has converged back to SAFE_DUTY
        # with no override owed emits nothing and drops out of tracking.
        profiled = {p.channel_id for p in self._profiles}
        synthetic = (set(held) | self._synthetic_live) - profiled
        for channel_id in sorted(synthetic):
            target = held.get(channel_id, SAFE_DUTY)
            ...  # mirror the profile-loop body (previous/cold/limit/converging)
```

Track `self._synthetic_live: set[str]` (initialized in `__init__`): a channel
enters when first held, leaves when it has no override AND its emitted duty
has reached `SAFE_DUTY` (check after computing `duty`/`converging`). Reuse the
profile-loop body — extract the shared per-channel emission logic into a
private helper (e.g. `_emit_for(channel_id, target, now) -> Intent | None`)
rather than duplicating the previous/cold/limit/deadband block twice; both
loops call it. Keep `mark_emitted` untouched (it is already channel-keyed).

- [ ] **Step 4: Run the full scheduler + engine suite**

Run: `cd services/control_engine && uv run pytest -v`
Expected: new tests PASS; every pre-existing test passes UNMODIFIED.

- [ ] **Step 5: check.sh and commit**

```bash
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh
git add services/control_engine
git commit -m "feat(engine): overrides command adopted channels with no profile

A held override on an unprofiled channel was stored, re-armed and
expired but never emitted - due() iterated configured profiles only.
Unprofiled held channels now behave as a constant-SAFE_DUTY schedule
the override outranks: held duty while owed, slew back to dark on
release, quiet after convergence. Spec 2026-08-15."
```

---

### Task 2: engine app-level proof + PR/CI/deploy gate

**Files:**
- Test: `services/control_engine/tests/test_app.py` or `tests/test_overrides.py` (extend — read both; put the test beside the existing override-flow tests)
- No production files expected.

**Interfaces:**
- Consumes: Task 1's scheduler behavior; the engine app's existing test harness for override flow (however test_overrides.py drives `_held`/`due`/publisher — follow it).

- [ ] **Step 1:** Add one app-level test following the file's existing override-flow idiom: an active override for an adopted, unprofiled channel results in a published command intent for that channel (and, if the harness makes it cheap, that an expired override leads to a publish of duty 0). Skip gracefully into the existing env-gate pattern if the chosen file is integration-gated — but prefer the unit-style file if one covers this layer.
- [ ] **Step 2:** RED → implement nothing (Task 1 should make it pass; if it does NOT, the gap is in how the app feeds `due()` — fix minimally and say so) → GREEN.
- [ ] **Step 3:** Full `cd services/control_engine && uv run pytest`, then check.sh; commit (`test(engine): app-level proof that unprofiled holds publish`).
- [ ] **Step 4 (controller gate, procedural):** push branch, PR, CI green, merge, `./scripts/deploy-pi.sh` (registry has devices? — post-reset walkthrough state applies; if the registry is empty at deploy time use `--no-verify` and say so), confirm engine logs show the held channel emitting when a test override is placed via curl… NO — placing overrides is the acceptance test David runs from the app. The deploy gate here is: services healthy + contracts 3.7.0 + (if devices exist) telemetry on the wire.

---

## Self-Review Notes

- Spec Feature 1 fully covered by Tasks 1-2; Feature 2 is the iOS plan.
- No contract change anywhere — Global Constraints enforce it.
- The `_synthetic_live` set is the one new piece of state; its lifecycle (enter on hold, exit on converged-quiet) is pinned by `test_release_slews_back_to_safe_and_goes_quiet`.

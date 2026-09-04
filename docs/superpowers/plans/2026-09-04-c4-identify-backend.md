# C4 Identify-before-adopt, backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an identify pulse distinguishable from a manual hold in the audit trail by writing the override request's `reason` into the `override.created` audit detail.

**Architecture:** The spec's ruling is that identify is an ordinary manual hold through `POST /api/v1/overrides`, so hardware-io, control-engine, the contracts and the database change nothing. The one gap is that `create_override` in the API drops `body.reason` on the floor when it writes the `override.created` audit row, so a 50 % identify pulse and a 50 % manual hold read identically in the trail. This plan adds that one key, taken from the request because `ActiveOverride` does not carry a reason, and one test.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, pytest against a loopback PostgreSQL (`BELLASREEF_TEST_DATABASE_URL`).

**Spec:** `docs/superpowers/specs/2026-09-03-identify-before-adopt-design.md`, sections "The pulse" (Audit paragraph) and "Testing" (API paragraph).

## Global Constraints

- `AuditEvent.event` is `dict[str, Any]`; adding a key is not an OpenAPI change. **No contracts bump, no migration, no new endpoint.**
- The `reason` value comes from `body.reason` (the `OverrideRequest`), never from the placed `ActiveOverride`, which has no reason field.
- When the request omits `reason`, the audit detail key is absent or `null`. Either is acceptable to the spec; this plan chooses to always write the key (`null` when omitted) so the detail shape is stable.
- `mypy --strict` clean, ruff lint and format clean.
- The API integration tests need a loopback PostgreSQL. This workstation has no container runtime, so the new test **cannot run locally**; run the module with `BELLASREEF_ALLOW_ENV_SKIPS=1` to confirm it collects and skips cleanly, and let CI run it. Say so in the report. Never point the tests at the hub.
- Conventional commits. Every commit message ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BNfMThZQTKBdgXs6DNvWEK
  ```
- Do not push, do not open the PR. The controller does that after review.

---

### Task 1: `override.created` carries the request's `reason`

**Files:**
- Modify: `services/api/bellasreef_api/app.py` (the `await sink("override.created", {...})` call inside `create_override`, around line 2512)
- Test: `services/api/tests/test_stream_and_overrides.py` (class `TestOverrides`, add one method after `test_transition_defaults_to_ramp_and_snap_round_trips`)

**Interfaces:**
- Consumes: `OverrideRequest.reason: str | None` (already on the request model, `app.py` line 671), the `sink` audit callable and `actor_fields` already used in `create_override`.
- Produces: the `override.created` audit detail gains the key `"reason"`. The iOS plan reads nothing from this; it exists for the audit trail.

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/test_stream_and_overrides.py`, inside the same class as `test_transition_defaults_to_ramp_and_snap_round_trips`, directly after it (same indentation). It follows that test's shape exactly: fresh engine, seeded devices, `Audit` sink, paired headers, two posts, then assert on the collected `override.created` details.

```python
    def test_the_request_reason_lands_in_the_created_audit_detail(self) -> None:
        # An identify pulse (spec 2026-09-03, C4) is an ordinary hold at 50 %;
        # the only thing that tells it apart from a manual 50 % hold in the
        # trail is the request's reason, which this endpoint used to drop.
        async def scenario() -> list[dict[str, Any]]:
            engine = await fresh_engine()
            await seed_devices(engine)
            audit = Audit()
            app = build_app(engine, audit=audit)
            headers = await paired(engine, app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                with_reason = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={
                        "target": "led-blue",
                        "duty": 0.5,
                        "duration_s": 5,
                        "transition": "snap",
                        "reason": "identify",
                    },
                )
                assert with_reason.status_code == 200, with_reason.text
                without = await c.post(
                    "/api/v1/overrides",
                    headers=headers,
                    json={"target": "led-blue", "duty": 0.5, "duration_s": 5},
                )
                assert without.status_code == 200, without.text
            await engine.dispose()
            return [d for e, d, _ in audit.records if e == "override.created"]

        details = run(scenario)
        assert [d.get("reason") for d in details] == ["identify", None]
        # The key is always present so the detail shape does not depend on
        # whether the client sent one.
        assert all("reason" in d for d in details)
```

- [ ] **Step 2: Run the test to verify it fails**

Run from the repo root:

```bash
cd services/api && uv run pytest tests/test_stream_and_overrides.py -k the_request_reason -v
```

With a loopback PostgreSQL (CI, or a local `BELLASREEF_TEST_DATABASE_URL`): expected FAIL at the first assert with `AssertionError: assert [None, None] == ['identify', None]`.

Without one (this workstation): expected `SKIPPED (BELLASREEF_TEST_DATABASE_URL not set)`. Run it as:

```bash
cd services/api && BELLASREEF_ALLOW_ENV_SKIPS=1 uv run pytest tests/test_stream_and_overrides.py -k the_request_reason -v
```

and record in the report that the RED leg could not be observed locally and is CI's to observe.

- [ ] **Step 3: Write the minimal implementation**

In `services/api/bellasreef_api/app.py`, inside `create_override`, change the `override.created` sink call. Before:

```python
        await sink(
            "override.created",
            {
                "override_id": str(placed.id),
                "target": placed.target,
                "duty": placed.duty,
                "expires_at": placed.expires_at.isoformat(),
                **await actor_fields(actor),
                "transition": placed.transition,
            },
            category="command",
        )
```

After:

```python
        await sink(
            "override.created",
            {
                "override_id": str(placed.id),
                "target": placed.target,
                "duty": placed.duty,
                "expires_at": placed.expires_at.isoformat(),
                **await actor_fields(actor),
                "transition": placed.transition,
                # From the request, not the placed row: ActiveOverride carries
                # no reason. This is what tells an identify pulse (C4) apart
                # from a manual hold at the same duty in the trail.
                "reason": body.reason,
            },
            category="command",
        )
```

- [ ] **Step 4: Run the test and the module to verify they pass**

```bash
cd services/api && uv run pytest tests/test_stream_and_overrides.py -v
```

With PostgreSQL: expected all tests in the module PASS, including the new one. Without: all skip under `BELLASREEF_ALLOW_ENV_SKIPS=1`, zero errors, zero failures.

Then the static gate, from the repo root, each as its own command:

```bash
uv run ruff check services/api
uv run ruff format --check services/api
uv run mypy --strict services/api
```

Expected: all three clean.

- [ ] **Step 5: Commit**

```bash
git add services/api/bellasreef_api/app.py services/api/tests/test_stream_and_overrides.py
git commit -m "feat(api): override.created audit detail carries the request reason (C4)

An identify pulse is an ordinary 50 % hold through POST /api/v1/overrides
(spec 2026-09-03). The only thing that distinguishes it from a manual hold
at the same duty in the audit trail is the request's reason, which
create_override dropped. Write it into the override.created detail, taken
from the request because ActiveOverride has no reason field. Not a contracts
change: AuditEvent.event is dict[str, Any].

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BNfMThZQTKBdgXs6DNvWEK"
```

---

## After the plan (controller work, not a task)

CI green on the PR, merge, tag `v0.2.3`, release workflow green, `update-hub.sh` on coco, telemetry verified on the wire. The iOS C4 plan (`docs/superpowers/plans/2026-09-04-c4-identify-ios.md`) does not depend on this change being deployed; the two ship independently.

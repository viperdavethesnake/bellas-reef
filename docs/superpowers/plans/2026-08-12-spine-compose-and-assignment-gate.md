# Spine-to-Compose Migration + Engine Assignment Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish yesterday's factory wipe properly: stop the control-engine from commanding channels no operator has adopted (the `led-blue` refusal loop), and replace the three ad-hoc `-dev` spine containers with the pinned `deploy/compose.yaml` spine on fresh volumes.

**Architecture:** Part 1 teaches the engine to consume the same retained `bellasreef.assignment.<device_id>` stream hardware-io already builds from — an intent for an unadopted channel is suppressed with a metric and a log line instead of published into a refusal. Part 2 is an infrastructure cutover on the Pi: stop ad-hoc containers, bring up the compose spine under a small systemd unit ordered `After=time-sync.target`, fresh named volumes, Alembic from scratch, then `deploy-pi.sh` with its telemetry-on-the-wire verification as the stop condition.

**Tech Stack:** Python 3.13 / nats-py JetStream / Pydantic v2 / pytest; Docker Compose with digest-pinned images; systemd.

## Global Constraints

- `mypy --strict` clean, Ruff clean (`./scripts/check.sh` is the gate).
- Conventional commits; push to main only via CI-green PR (repo rule).
- **A backend pass is not done at CI green.** Stop condition: CI green → `scripts/deploy-pi.sh` → telemetry verified on the wire.
- Integration tests never touch the hub (loopback/CI only; `BELLASREEF_ALLOW_ENV_SKIPS=1` declares skips on the Mac, which has no container runtime).
- Every host-mutating command on the Pi is shown to David before running.
- Unit files stay machine-agnostic except the documented clone path `/home/david/bellasreef`.
- Fresh volumes ruling (David, 2026-08-12): the bench-era audit log and telemetry are abandoned; do NOT copy old volume data.

## Decisions already made (do not re-litigate during execution)

1. **Fresh volumes** for the compose spine (David's explicit call). Old containers are *stopped and kept* for rollback; removal is a separate, David-gated step.
2. **The gate lives in control-engine, on the assignment stream** — not a registry DB read (engine stays off tier-one hardware knowledge; the stream is the spoke-compatible contract), and not in hardware-io (it already refuses correctly; the defect is the engine asking).
3. `deploy/config/lighting.json` **stays in git unchanged**. Once the engine gates on assignments, a profile for an unadopted channel is inert by design (suppressed, visible in metrics) rather than a refusal loop.
4. **App services stay as systemd host units.** `deploy/compose.yaml` also declares containerized `hardware-io`/`control-engine`/`api`; starting them would double-consume the BR_CMD workqueue and collide on port 8000. Every compose invocation in this plan therefore names the spine services explicitly: `nats postgres victoria-metrics`. The architecture-vs-deployment conflict (CLAUDE.md "all containers" vs. systemd units) is **flagged to David in Task 11**, not resolved here.

## Known facts the executor should not re-derive

- Ad-hoc containers: `nats-dev` (created Aug 9, **no volume** — JetStream data dies with the container), `pg-dev` (Aug 10, anonymous volume, LAN-exposed 5432), `vm-dev` (Aug 10, volume `vm-dev-data`, LAN-exposed 8428). No compose labels; `restart=unless-stopped`.
- Host services reach the spine at `localhost:4222/5432/8428` (see `/etc/bellasreef/*.env`); compose maps NATS and VM to loopback already but **postgres has no host port mapping** — Task 5 adds one.
- Compose interpolation evaluates the whole file, so `${I2C_GID:?}`/`${GPIO_GID:?}` must exist in `deploy/.env` on the Pi even though the app services are never started (Task 8).
- `DeviceAssignment` (contracts/python/bellasreef_contracts/messages.py:241): fields `device_id`, `adopted: bool`, `role`, `driver_type`, `binding`; `adopted=False` is the unbind tombstone. Retained last-value per subject on `bellasreef.assignment.<device_id>`; wildcard `subjects.ALL_ASSIGNMENTS`.
- hardware-io's drain pattern to mirror: `services/hardware_io/bellasreef_hardware_io/spine.py:219` (`read_assignments`) — JS pull subscribe, `DeliverPolicy.LAST_PER_SUBJECT`, fetch loop, `NotFoundError` → not provisioned.
- The API republishes every assignment from Postgres at its own startup (`services/api/bellasreef_api/app.py:632`) and on every bind/unbind (`app.py:1202`).
- Engine suppression metric already exists: `_Metrics.suppressed` (`bellasreef_commands_suppressed_total`, one label), used with labels `no_spine`, `clock_untrusted`, `override_expired`.
- The refusal loop signature being fixed: audit `command_refused / rejected_unknown` for `led-blue` every ~5 min (the scheduler's `DEFAULT_REFRESH_S = 300`).

---

# Part 1 — control-engine assignment gate (code, TDD, Mac + CI)

## File Structure

- Create: `services/control_engine/bellasreef_control_engine/assignments.py` — `AssignmentLedger`, pure state, no I/O.
- Modify: `services/control_engine/bellasreef_control_engine/publisher.py` — `load_assignments()` (drain) and `subscribe_assignments()` (live).
- Modify: `services/control_engine/bellasreef_control_engine/app.py` — wire ledger into `run()` and gate `_tick()`.
- Create: `services/control_engine/tests/test_assignments.py`
- Modify: `services/control_engine/tests/test_app.py` (gate behavior tests — follow that file's existing fixtures for constructing `ControlEngine` and faking the publisher).

### Task 1: AssignmentLedger

**Files:**
- Create: `services/control_engine/bellasreef_control_engine/assignments.py`
- Test: `services/control_engine/tests/test_assignments.py`

**Interfaces:**
- Produces: `AssignmentLedger` with `apply(assignment: DeviceAssignment) -> None`, `adopted: frozenset[str]` (property), `is_adopted(device_id: str) -> bool`. Tasks 2–4 rely on exactly these names.

- [ ] **Step 1: Write the failing tests**

```python
# services/control_engine/tests/test_assignments.py
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The ledger is pure state: assignments in, adopted-set out."""

from datetime import UTC, datetime
from uuid import uuid4

from bellasreef_contracts import DeviceAssignment
from bellasreef_control_engine.assignments import AssignmentLedger


def _assignment(device_id: str, *, adopted: bool) -> DeviceAssignment:
    kwargs: dict = {}
    if adopted:
        kwargs = {"driver_type": "pi-pwm", "binding": {"channel": "0"}, "role": "light"}
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=adopted,
        **kwargs,
    )


class TestAssignmentLedger:
    def test_starts_empty(self) -> None:
        assert AssignmentLedger().adopted == frozenset()

    def test_adoption_adds(self) -> None:
        ledger = AssignmentLedger()
        ledger.apply(_assignment("led-blue", adopted=True))
        assert ledger.is_adopted("led-blue")
        assert ledger.adopted == frozenset({"led-blue"})

    def test_tombstone_removes(self) -> None:
        ledger = AssignmentLedger()
        ledger.apply(_assignment("led-blue", adopted=True))
        ledger.apply(_assignment("led-blue", adopted=False))
        assert not ledger.is_adopted("led-blue")

    def test_tombstone_for_unknown_device_is_a_no_op(self) -> None:
        ledger = AssignmentLedger()
        ledger.apply(_assignment("led-blue", adopted=False))
        assert ledger.adopted == frozenset()
```

Note: if `DeviceAssignment`'s `_Message` base requires fields beyond `message_id`/`emitted_at`/`source`, copy the construction pattern from the existing contract tests (`grep -rn "DeviceAssignment(" contracts services --include="*.py"`) rather than guessing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/control_engine/tests/test_assignments.py -v`
Expected: FAIL — `ModuleNotFoundError: bellasreef_control_engine.assignments`

- [ ] **Step 3: Implement**

```python
# services/control_engine/bellasreef_control_engine/assignments.py
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Which devices an operator has adopted, per the retained assignment stream.

The engine's half of the contract hardware-io already honours: assignments are
tier two of the registry, on the wire (see DeviceAssignment in the contracts
package). hardware-io builds drivers from them; the engine consults them so it
never commands a channel nobody has adopted. Same stream, same tombstone
semantics, no database dependency.
"""

from __future__ import annotations

from bellasreef_contracts import DeviceAssignment

__all__ = ["AssignmentLedger"]


class AssignmentLedger:
    """Pure state. Feeding it is the publisher's job; consulting it is the tick's."""

    def __init__(self) -> None:
        self._adopted: set[str] = set()

    @property
    def adopted(self) -> frozenset[str]:
        return frozenset(self._adopted)

    def is_adopted(self, device_id: str) -> bool:
        return device_id in self._adopted

    def apply(self, assignment: DeviceAssignment) -> None:
        if assignment.adopted:
            self._adopted.add(assignment.device_id)
        else:
            # adopted=False is the tombstone: the channel is free again.
            self._adopted.discard(assignment.device_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/control_engine/tests/test_assignments.py -v`
Expected: 4 PASS

- [ ] **Step 5: Lint/type gate, then commit**

Run: `uv run ruff check services/control_engine && uv run mypy services/control_engine`

```bash
git add services/control_engine/bellasreef_control_engine/assignments.py services/control_engine/tests/test_assignments.py
git commit -m "feat(engine): assignment ledger, the engine's view of adoption"
```

### Task 2: Publisher learns to feed the ledger

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/publisher.py`

**Interfaces:**
- Consumes: `AssignmentLedger.apply` from Task 1.
- Produces: `CommandPublisher.load_assignments(ledger: AssignmentLedger) -> bool` (drain retained stream once; `False` if the stream is not provisioned yet) and `CommandPublisher.subscribe_assignments(handler: Callable[[DeviceAssignment], None]) -> None` (core subscription on `subjects.ALL_ASSIGNMENTS`). Task 3 relies on these exact signatures.

- [ ] **Step 1: Write the failing test**

Add to `services/control_engine/tests/test_assignments.py` (drain logic is exercised against fakes; the real broker path is covered by the loopback integration suite in CI):

```python
import asyncio

from bellasreef_control_engine.publisher import CommandPublisher


class _FakeMsg:
    def __init__(self, payload: bytes, subject: str) -> None:
        self.data = payload
        self.subject = subject
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class _FakeSub:
    def __init__(self, batches: list[list[_FakeMsg]]) -> None:
        self._batches = batches

    async def fetch(self, n: int, timeout: float) -> list[_FakeMsg]:
        if not self._batches:
            raise TimeoutError
        return self._batches.pop(0)

    async def unsubscribe(self) -> None:
        pass


class _FakeJs:
    def __init__(self, batches: list[list[_FakeMsg]]) -> None:
        self._batches = batches

    async def pull_subscribe(self, subject: str, durable: object, config: object) -> _FakeSub:
        return _FakeSub(self._batches)


def test_drain_feeds_ledger_and_skips_garbage() -> None:
    good = _assignment("led-blue", adopted=True)
    msgs = [
        _FakeMsg(good.model_dump_json().encode(), "bellasreef.assignment.led-blue"),
        _FakeMsg(b"not json", "bellasreef.assignment.junk"),
    ]
    publisher = CommandPublisher("nats://unused:4222")
    publisher._js = _FakeJs([msgs])  # type: ignore[assignment]
    ledger = AssignmentLedger()

    loaded = asyncio.run(publisher.load_assignments(ledger))

    assert loaded is True
    assert ledger.adopted == frozenset({"led-blue"})
    assert all(m.acked for m in msgs)
```

If `test_app.py` or the hardware-io tests already have a fake-JetStream helper, reuse that instead of these local fakes.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/control_engine/tests/test_assignments.py -v -k drain`
Expected: FAIL — `AttributeError: 'CommandPublisher' object has no attribute 'load_assignments'`

- [ ] **Step 3: Implement, mirroring hardware-io's `read_assignments` (spine.py:219)**

Add to `publisher.py` (imports: `DeviceAssignment` from `bellasreef_contracts`, `ConsumerConfig`, `DeliverPolicy` from `nats.js.api`, `NotFoundError` from `nats.js.errors`, `ValidationError` already imported; `AssignmentLedger` from `.assignments`):

```python
async def load_assignments(self, ledger: AssignmentLedger) -> bool:
    """Drain the retained assignment stream into ``ledger``, once.

    Mirrors hardware-io's startup read: LAST_PER_SUBJECT gives the current
    truth per device, tombstones included. Returns False when the stream is
    not provisioned yet — a hub booting in arbitrary order — so the caller
    knows to retry rather than trusting an empty ledger forever.
    """
    if self._js is None:
        raise RuntimeError("publisher not connected")
    try:
        sub = await self._js.pull_subscribe(
            subjects.ALL_ASSIGNMENTS,
            durable=None,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.LAST_PER_SUBJECT),
        )
    except NotFoundError:
        log.warning("assignment stream not provisioned yet; will retry")
        return False

    while True:
        try:
            msgs = await sub.fetch(32, timeout=1.0)
        except (TimeoutError, nats.errors.TimeoutError):
            break
        for msg in msgs:
            try:
                ledger.apply(DeviceAssignment.model_validate_json(msg.data))
            except ValidationError:
                log.warning("assignment did not validate; skipped", extra={"subject": msg.subject})
            await msg.ack()
    with contextlib.suppress(Exception):
        await sub.unsubscribe()
    log.info("assignments loaded", extra={"adopted": sorted(ledger.adopted)})
    return True


async def subscribe_assignments(self, handler: Callable[[DeviceAssignment], None]) -> None:
    """Live adoption changes, on core pub/sub.

    A JetStream publish traverses core subjects too, so a plain subscription
    hears every bind/unbind the API publishes — no durable, deliberately:
    a durable here would contend with nothing but would still be broker
    state to leak. Malformed payloads are dropped with a log, same contract
    as subscribe_sensors.
    """
    if self._nc is None:
        raise RuntimeError("publisher not connected")

    async def _on_message(msg: Msg) -> None:
        try:
            handler(DeviceAssignment.model_validate_json(msg.data))
        except ValidationError:
            log.warning("dropping an undecodable assignment", extra={"subject": msg.subject})

    await self._nc.subscribe(subjects.ALL_ASSIGNMENTS, cb=_on_message)
    log.info("subscribed to assignments", extra={"subject": subjects.ALL_ASSIGNMENTS})
```

- [ ] **Step 4: Run tests, then the full engine suite**

Run: `uv run pytest services/control_engine -v`
Expected: all PASS (pre-existing tests untouched).

- [ ] **Step 5: Lint/type gate, then commit**

```bash
git add services/control_engine/bellasreef_control_engine/publisher.py services/control_engine/tests/test_assignments.py
git commit -m "feat(engine): drain and follow the assignment stream"
```

### Task 3: The gate in `_tick`

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`__init__`, `run`, `_tick`)
- Test: `services/control_engine/tests/test_app.py`

**Interfaces:**
- Consumes: `AssignmentLedger` (Task 1), `load_assignments`/`subscribe_assignments` (Task 2).
- Produces: `ControlEngine.assignments: AssignmentLedger` attribute (tests and Task 4 use it to seed adoption).

- [ ] **Step 1: Write the failing tests**

Add to `test_app.py`, following that file's existing pattern for building a `ControlEngine` with a captured/fake publisher (read the file first; adapt the fixture names, not the assertions):

```python
def test_unadopted_channel_is_suppressed_not_published(engine_with_fake_publisher) -> None:
    """A profile for a channel nobody adopted must produce zero commands."""
    engine, published = engine_with_fake_publisher  # profiles include "led-blue"
    asyncio.run(engine._tick(datetime.now(UTC)))
    assert published == []


def test_adopted_channel_publishes(engine_with_fake_publisher) -> None:
    engine, published = engine_with_fake_publisher
    engine.assignments.apply(_assignment("led-blue", adopted=True))
    asyncio.run(engine._tick(datetime.now(UTC)))
    assert [c.actuator_id for c in published] == ["led-blue"]


def test_adoption_mid_run_starts_cold_from_safe_duty(engine_with_fake_publisher) -> None:
    """Suppressed ticks must not mark_emitted: the first real command after
    adoption is the cold 'initial' intent slewing up from SAFE_DUTY, not a
    mid-ramp jump."""
    engine, published = engine_with_fake_publisher
    asyncio.run(engine._tick(datetime.now(UTC)))  # suppressed
    engine.assignments.apply(_assignment("led-blue", adopted=True))
    asyncio.run(engine._tick(datetime.now(UTC)))
    assert published[0].reason == "lighting:initial"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/control_engine/tests/test_app.py -v -k adopt`
Expected: FAIL — `AttributeError: 'ControlEngine' object has no attribute 'assignments'`

- [ ] **Step 3: Implement**

In `__init__` (after `self.publisher = ...`, app.py:133):

```python
        self.assignments = AssignmentLedger()
        self._assignments_loaded = False
        self._suppressed_unassigned: set[str] = set()
```

In `run()` (after `await self.publisher.connect()`, app.py:164):

```python
        if self.publisher is not None:
            await self.publisher.subscribe_assignments(self.assignments.apply)
            self._assignments_loaded = await self.publisher.load_assignments(self.assignments)
```

In `_loop()` (inside the `while`, before `_tick` — retry a drain that found no stream, so boot order cannot leave the engine deaf; the live subscription alone would miss anything published before it attached):

```python
            if self.publisher is not None and self.publisher.connected and not self._assignments_loaded:
                self._assignments_loaded = await self.publisher.load_assignments(self.assignments)
```

In `_tick` (app.py:334), replace the publish loop:

```python
        intents = self.scheduler.due(now, held)
        for intent in intents:
            if not self.assignments.is_adopted(intent.channel_id):
                # Not an error and not silent: the schedule is config-in-git,
                # adoption is operator state, and the two are allowed to
                # disagree — a profile for a channel nobody has adopted waits.
                # One log per channel, a metric forever, zero commands: the
                # alternative was a command_refused audit row every 5 minutes.
                self.metrics.suppressed.labels("unassigned").inc()
                if intent.channel_id not in self._suppressed_unassigned:
                    self._suppressed_unassigned.add(intent.channel_id)
                    log.warning(
                        "channel has a schedule but no adoption; holding",
                        extra={"channel_id": intent.channel_id},
                    )
                continue
            if intent.channel_id in self._suppressed_unassigned:
                self._suppressed_unassigned.discard(intent.channel_id)
                log.info("channel adopted; scheduling resumes", extra={"channel_id": intent.channel_id})
            await self._publish(intent, now)
```

Import `AssignmentLedger` at the top of app.py.

- [ ] **Step 4: Run the full engine suite**

Run: `uv run pytest services/control_engine -v`
Expected: all PASS — including pre-existing `_tick` tests. **If existing tests published without any adoption seeding, they will now fail; fix them by seeding `engine.assignments` in their fixtures, because the new default (no adoption → no commands) is the intended behavior change.**

- [ ] **Step 5: Full check gate, then commit**

Run: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh`

```bash
git add services/control_engine
git commit -m "feat(engine): no commands for channels nobody adopted"
```

### Task 4: Land Part 1

- [ ] **Step 1: Push, CI green** (PR per repo rule; merge to main). Note deployment happens once, at the end of the cutover (Task 9) — deploying Part 1 alone is fine but redundant.

---

# Part 2 — spine cutover to compose (infra; every Pi command shown to David first)

### Task 5: compose.yaml — postgres loopback port

**Files:**
- Modify: `deploy/compose.yaml` (postgres service, after `environment:` block)

- [ ] **Step 1: Add the mapping**

```yaml
    ports:
      # Phase-1 topology runs api/control-engine/hardware-io as systemd host
      # units (see deploy/systemd/); they reach Postgres on loopback. Remove
      # when the app services move into this file for real.
      - "127.0.0.1:5432:5432"
```

- [ ] **Step 2: Validate rendering**

Run: `cd deploy && POSTGRES_USER=x POSTGRES_PASSWORD=x POSTGRES_DB=x BELLASREEF_DATABASE_URL=x I2C_GID=0 GPIO_GID=0 docker compose config --quiet 2>/dev/null; cd ..`
(No container runtime on the Mac is fine — if `docker` is absent, defer validation to the Pi in Task 9 and say so.)

- [ ] **Step 3: Commit**

```bash
git add deploy/compose.yaml
git commit -m "feat(deploy): loopback postgres port for the host-unit topology"
```

### Task 6: bellasreef-spine.service

**Files:**
- Create: `deploy/systemd/bellasreef-spine.service`

- [ ] **Step 1: Write the unit**

```ini
# Bella's Reef — the spine (nats, postgres, victoria-metrics) as a compose
# stack. Explicit service list: compose.yaml also declares the app services,
# which run as host units in phase 1 — `up` without the list would start a
# second BR_CMD consumer and collide on port 8000.
#
# After=time-sync.target is the clock-trust opt-in the compose file's
# hardware-io comment promises: containers have no timedatectl, so the host
# orders the stack after synchronisation instead.
[Unit]
Description=Bella's Reef spine (nats, postgres, victoria-metrics)
Requires=docker.service
After=docker.service network-online.target time-sync.target
Wants=network-online.target time-sync.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=david
WorkingDirectory=/home/david/bellasreef/deploy
ExecStart=/usr/bin/docker compose up -d --wait nats postgres victoria-metrics
ExecStop=/usr/bin/docker compose stop nats postgres victoria-metrics

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Commit**

```bash
git add deploy/systemd/bellasreef-spine.service
git commit -m "feat(deploy): spine compose stack under systemd, clock-ordered"
```

### Task 7: deploy-pi.sh knows about the spine

**Files:**
- Modify: `scripts/deploy-pi.sh`

- [ ] **Step 1: Read the script end-to-end first** (13 KB). Then:
  - The existing unit-install line (`sudo install -m 0644 ${PI_DIR}/deploy/systemd/*.service ...`, line ~130) already copies the new unit — verify the glob, change nothing if it covers it.
  - Where units are **enabled** (line ~161): add `bellasreef-spine.service` to the enable list.
  - Where units are **restarted** (line ~167): do **NOT** add the spine unit. Instead, immediately before the restart of the app units, add an idempotent start with a comment:

```bash
# The spine is started, never restarted, by a deploy: restarting it would
# bounce Postgres and NATS under every code push for no reason. `start` on an
# already-active oneshot with RemainAfterExit is a no-op.
ssh "$PI_HOST" "sudo systemctl start bellasreef-spine.service" || die "spine failed to start"
```

  - Confirm the migration step runs **after** that start (fresh DB must exist before Alembic).

- [ ] **Step 2: Shellcheck**

Run: `shellcheck scripts/deploy-pi.sh` (if shellcheck is absent locally, note it and rely on reading the diff carefully).

- [ ] **Step 3: Commit, push, CI green**

```bash
git add scripts/deploy-pi.sh
git commit -m "feat(deploy): deploy starts the spine unit, never restarts it"
```

### Task 8: `deploy/.env` on the Pi (host state, not committed)

- [ ] **Step 1: Read the current DB credentials** from `/etc/bellasreef/api.env` (`BELLASREEF_DATABASE_URL=postgresql+asyncpg://bellasreef:<pw>@localhost:5432/bellasreef`) — the compose Postgres must be initialized with the **same** user/password/db so the service env files need no change.

- [ ] **Step 2: Create the file** (show David first; heredoc over ssh, values filled from step 1 and `getent`):

```bash
ssh <pi> 'cat > /home/david/bellasreef/deploy/.env <<EOF
POSTGRES_USER=bellasreef
POSTGRES_PASSWORD=<from api.env>
POSTGRES_DB=bellasreef
BELLASREEF_DATABASE_URL=postgresql+asyncpg://bellasreef:<from api.env>@localhost:5432/bellasreef
VM_RETENTION=24
I2C_GID=$(getent group i2c | cut -d: -f3)
GPIO_GID=$(getent group gpio | cut -d: -f3)
EOF
chmod 600 /home/david/bellasreef/deploy/.env'
```

Note: `deploy/.env` is git-ignored; `deploy-pi.sh` resets the tree with `git reset`, which leaves ignored files alone — verify that assumption against the script while executing Task 7.

- [ ] **Step 3: Validate compose renders on the Pi**

Run: `ssh <pi> 'cd /home/david/bellasreef/deploy && docker compose config --quiet && echo OK'`

### Task 9: The cutover (runbook — one step at a time, each shown to David)

Preconditions: Tasks 1–8 merged and CI green; David available (brief hub outage; nothing depends on it — bench hardware, no livestock).

- [ ] **Step 1: Stop the app units**

```bash
ssh <pi> 'sudo systemctl stop bellasreef-api bellasreef-control-engine bellasreef-hardware-io'
```

- [ ] **Step 2: Retire the ad-hoc containers — stop, strip restart policy, KEEP for rollback**

```bash
ssh <pi> 'docker update --restart=no pg-dev nats-dev vm-dev && docker stop pg-dev nats-dev vm-dev'
```

- [ ] **Step 3: Deploy.** `./scripts/deploy-pi.sh` — which now: resets the Pi checkout to main (clearing the current 4-commit docs drift), installs and enables all units including the spine, starts the spine (compose pulls by digest, fresh named volumes `bellasreef_postgres-data`/`bellasreef_nats-data`/`bellasreef_vm-data`, ICU collation from initdb), applies Alembic migrations to the empty database, restarts the app units, and verifies fresh telemetry on the wire. If the script dies at any stage, STOP and show David the output — the rollback is Step 6, not improvisation.

- [ ] **Step 4: Verify the cutover did what it claims**

```bash
curl -s http://bellasreef.local:8000/api/v1/info
#   expect: paired_client_count 0, pairing_open true, contracts 3.5.0
ssh <pi> 'docker ps --format "{{.Names}} {{.Status}}"'
#   expect: bellasreef-nats-1 / bellasreef-postgres-1 / bellasreef-victoria-metrics-1, healthy; the -dev trio absent
ssh <pi> 'docker exec bellasreef-postgres-1 psql -U bellasreef -d bellasreef -c "SELECT count(*) FROM capabilities"'
#   expect: 5 (hardware-io re-announced on restart)
```

- [ ] **Step 5: Verify the refusal loop is dead (the whole point).** Wait ≥ 11 minutes (two 5-minute schedule refreshes), then:

```bash
ssh <pi> 'docker exec bellasreef-postgres-1 psql -U bellasreef -d bellasreef -c \
  "SELECT count(*) FROM audit_log WHERE category='"'"'command'"'"'"'
#   expect: 0 — no command_refused rows, ever, on the fresh database
curl -s http://bellasreef.local:9102/metrics 2>/dev/null | grep 'suppressed.*unassigned' \
  || ssh <pi> 'curl -s http://localhost:9102/metrics | grep unassigned'
#   expect: bellasreef_commands_suppressed_total{reason="unassigned"} climbing — suppressed, visibly, not refused
```

- [ ] **Step 6 (only on failure): Rollback**

```bash
ssh <pi> 'sudo systemctl stop bellasreef-api bellasreef-control-engine bellasreef-hardware-io && sudo systemctl disable --now bellasreef-spine'
ssh <pi> 'docker update --restart=unless-stopped pg-dev nats-dev vm-dev && docker start pg-dev nats-dev vm-dev'
ssh <pi> 'sudo systemctl start bellasreef-api bellasreef-control-engine bellasreef-hardware-io'
```

### Task 10: Gated cleanup (needs David's explicit go, after he has paired and is satisfied)

- [ ] **Step 1: Remove the retired containers and their volumes** — destructive, irreversible, David says go first:

```bash
ssh <pi> 'docker rm pg-dev nats-dev vm-dev && docker volume rm vm-dev-data && docker volume prune -f --filter label!=keep'
```

(The anonymous pg-dev volume `4f3ad52…` is caught by the prune; name it explicitly instead if prune feels too broad: `docker volume rm 4f3ad5228b7c…`.)

### Task 11: Docs + the flags David gets in writing

**Files:**
- Modify: `CLAUDE.md` (Deployment discipline + Verified host facts)
- Modify: `docs/host-setup.md`

- [ ] **Step 1: CLAUDE.md** — in "Deployment discipline", add one bullet: the spine runs as `bellasreef-spine.service` (compose, explicit service list `nats postgres victoria-metrics`, started-never-restarted by deploys, `After=time-sync.target`); `deploy/.env` on the Pi is host state alongside `/etc/bellasreef/*.env`. In "Verified host facts", replace any stale container references with the compose names, and record the 2026-08-12 cutover date.

- [ ] **Step 2: docs/host-setup.md** — add `deploy/.env` to the documented host mutations (it is now the second one, next to dtoverlays).

- [ ] **Step 3: Record the two standing flags** at the end of the CLAUDE.md deployment section, so they cannot be silently resolved later:

```markdown
- FLAG (2026-08-12, unresolved): compose.yaml declares containerized app
  services; the operative deployment is systemd host units. One of the two is
  the future; David decides which, deliberately, not mid-task.
- FLAG (2026-08-12, unresolved): the ad-hoc `-dev` spine ran LAN-exposed
  (0.0.0.0:5432/4222/8428) since Aug 9; the compose spine is loopback-only.
  If anything off-Pi was talking to those ports directly, it broke at cutover
  — nothing is known to, but the exposure existed for three days.
```

- [ ] **Step 4: Commit and push (docs-only; deploy not required, but running `deploy-pi.sh` again is harmless and keeps the drift rule clean)**

```bash
git add CLAUDE.md docs/host-setup.md
git commit -m "docs: spine unit, deploy/.env host state, and two standing flags"
```

---

## Self-Review (done at write time)

- **Coverage:** refusal loop → Tasks 1–4 (gate) + Task 9 Step 5 (proof); ad-hoc containers → Tasks 5–10; 4-commit drift → cleared by Task 9 Step 3; audit-noise stop verified on fresh DB.
- **Types:** `AssignmentLedger.apply/is_adopted/adopted` used identically in Tasks 1/2/3; `load_assignments -> bool` consumed in Task 3's `_loop` retry.
- **Known uncertainty, stated rather than hidden:** the exact fixture shape in `test_app.py` (Task 3 Step 1 says adapt fixture names, keep assertions); `_Message` required fields (Task 1 Step 1 says copy the existing construction pattern); whether `deploy-pi.sh`'s reset spares ignored files (Task 8 Step 2 says verify during Task 7's read).

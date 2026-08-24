# Heartbeat Safety Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make heartbeat-loss safe-state protection real end-to-end — the engine beats, hardware-io listens, every production actuator has a live watcher, a failed safe-drive retries instead of silently dying, and a hardware-io restart no longer lets the engine snap a dark channel to full duty.

**Architecture:** Three links ship together (2026-08-23 code review, finding 1): (1) control-engine publishes a `Heartbeat` on `bellasreef.heartbeat.control-engine` every loop tick while its clock is trusted; (2) hardware-io core-subscribes to that subject and feeds `InterlockSupervisor.heartbeat()`; (3) `InterlockSupervisor.register()` spawns the heartbeat watcher itself when registration happens after `start()` — which is every production actuator, since `app.run()` calls `supervisor.start()` before `_connect_spine()` builds from the registry. Shipping any one link alone either leaves the protection dead or trips every actuator dark every 30 s. A fourth piece (finding 6) closes the loop the other way: the engine subscribes to `bellasreef.state.>` and forgets a channel's emission history when hardware-io reports an autonomous safe-state transition, so recovery converges from dark under the slew instead of snapping.

**Tech Stack:** Python 3.13, asyncio, nats-py 2.15 (core pub/sub for heartbeats — never JetStream), Pydantic v2 contracts (`Heartbeat`, `ActuatorState`, `subjects.heartbeat`, `subjects.ALL_STATE` — all already exist; **no contracts version bump**), pytest.

**Spec:** The findings themselves — `services/` code review 2026-08-23, findings 1, 2, 6 — plus CLAUDE.md "Safety is architecture" and safety.py's module docstring, which already documents the intended semantics (heartbeat loss drives safe, does not latch, never springs back on by itself).

## Global Constraints

- Python 3.13+, fully typed, `mypy --strict` clean. Ruff for lint/format.
- Heartbeats go over **core pub/sub, never JetStream** (a replayed heartbeat makes a dead controller look alive) — `Spine.publish_heartbeat` and `CommandPublisher.heartbeat` already enforce this.
- Nothing in `safety.py` may await anything network-bound (module docstring). The NATS subscription lives in `spine.py`/`app.py`; safety.py only ever sees the synchronous `heartbeat()` call.
- `safety.py` stays publish-blind: event emission goes through the existing `EventSink`; no new NATS dependency there.
- Conventional commits. All work on a feature branch, PR to main, CI must pass (lint, types, tests, multi-arch build).
- Run the full check locally before pushing: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` (this machine has no container runtime; integration suites skip declared, CI runs them).
- **Deploy is part of done:** CI green → `scripts/deploy-pi.sh` → telemetry verified on the wire. Hardware drills (Task 7) are pre-approved by David 2026-08-23 ("run any hardware drills you need, approved if you are capable") — except power-pull, which needs hands.

## Context a fresh engineer needs

- `services/hardware_io/bellasreef_hardware_io/safety.py` — `InterlockSupervisor`. `register()` (line ~170) adds a `_Guard`; `start()` (line ~202) drives all currently-registered actuators safe and spawns `_watch_heartbeat` tasks; **actuators registered after `start()` never get a watcher** (`watch_task=None` forever). `heartbeat()` (line ~265) stamps every guard — it has zero production callers today. `_runtime_deadline` (line ~366) sets `latched=True` then awaits `_drive_safe`; neither it nor `_watch_heartbeat` catches exceptions, so one driver `OSError` kills the task silently.
- `services/hardware_io/bellasreef_hardware_io/app.py` — `HardwareIO.run()` (line ~461): `supervisor.start()` at line ~473, `_connect_spine()` at ~487. `_connect_spine` builds from registry (registering actuators — after start), then `watch_assignments`, `_announce_capabilities`, registrations, `_publish_startup_states`, and finally `CommandConsumer.subscribe()`.
- `services/hardware_io/bellasreef_hardware_io/spine.py` — `Spine` has `publish_heartbeat` and a core-subscribe pattern to copy (`watch_assignments`, line ~294). No heartbeat subscription exists.
- `services/control_engine/bellasreef_control_engine/app.py` — `ControlEngine._loop()` (line ~235). `_refresh_clock_trust` already logs "suspending scheduling AND heartbeats" on clock loss (line ~614) — **the log claims a mechanism that was never wired**. `_on_reconnected` (line ~222).
- `services/control_engine/bellasreef_control_engine/publisher.py` — `CommandPublisher.heartbeat(interval_s)` (line ~316) exists, fully implemented, zero callers. `subscribe_assignments` (line ~288) is the core-subscribe pattern to copy for states. `connected` is `self._js is not None` (line ~131) — do NOT fix that here; it is batch C's finding 8. Use it as-is.
- `services/control_engine/bellasreef_control_engine/scheduler.py` — `LightingScheduler.forget(channel_id)` (line ~332) already exists (written for assignment tombstones) and does exactly what recovery needs: next intent is a cold start converging from `SAFE_DUTY` under the slew.
- Fakes for tests: `services/hardware_io/bellasreef_hardware_io/fakes.py` (`FakeActuator`). Existing test conventions: `services/hardware_io/tests/test_drills.py`, `test_actuator_state.py`; `services/control_engine/tests/test_app.py`, `test_publisher.py`.
- Deployment ordering note (record in PR body): after deploy, registry actuators have live 30 s watchers. During a deploy the engine may be down briefly; actuators trip to safe state (dark) and recover via the schedule once beats resume. That is the designed behavior, not a regression — a deploy now visibly cycles held/scheduled lights through dark for the gap.

---

### Task 1: Supervisor spawns watchers for late registrations

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/safety.py` (~line 170, `register()`)
- Test: `services/hardware_io/tests/test_safety.py` (extend; create if the supervisor's tests live elsewhere — check `grep -rl "InterlockSupervisor" services/hardware_io/tests/` and extend the file that already tests heartbeat behavior)

**Interfaces:**
- Produces: `InterlockSupervisor.register()` — unchanged signature; new behavior: when called while the supervisor is running, the guard's `last_beat` is stamped now and its `_watch_heartbeat` task is spawned immediately.

- [ ] **Step 1: Write the failing test**

```python
async def test_register_after_start_gets_a_watcher(fake_registration, fake_actuator):
    """A production actuator is registered AFTER start() (app.py builds from the
    registry only once the spine is up). It must still get a heartbeat watcher —
    2026-08-23 finding 1: every production actuator had watch_task=None."""
    events: list[SafetyEvent] = []

    async def sink(event: SafetyEvent) -> None:
        events.append(event)

    sup = InterlockSupervisor(on_event=sink)
    await sup.start()  # zero actuators, mirrors app.run() ordering

    sup.register(fake_registration(actuator_id="late", heartbeat_timeout_s=0.05), fake_actuator)
    # Drive it out of safe state so the trip is observable.
    await sup.apply(make_command("late", non_safe_level()))
    await asyncio.sleep(0.15)  # > heartbeat_timeout_s with no beats

    assert any(e.reason == "heartbeat_timeout" and e.actuator_id == "late" for e in events)
    assert fake_actuator.is_safe()
    await sup.stop()


async def test_late_registration_beats_keep_it_alive(fake_registration, fake_actuator):
    """Beats arriving via heartbeat() hold off the trip for a late-registered guard."""
    sup = InterlockSupervisor(on_event=_null_sink)
    await sup.start()
    sup.register(fake_registration(actuator_id="late", heartbeat_timeout_s=0.1), fake_actuator)
    await sup.apply(make_command("late", non_safe_level()))
    for _ in range(4):
        await asyncio.sleep(0.05)
        sup.heartbeat()
    assert not fake_actuator.is_safe()  # still at commanded level, no trip
    await sup.stop()
```

Adapt fixture names to what the existing supervisor tests use (there are existing tests calling `InterlockSupervisor.heartbeat()` — reuse their helpers for registrations, commands, and levels rather than inventing new ones).

- [ ] **Step 2: Run tests to verify the first fails and the second currently passes only vacuously**

Run: `uv run pytest services/hardware_io/tests -k "late" -v`
Expected: `test_register_after_start_gets_a_watcher` FAILS (no trip event — watcher never spawned).

- [ ] **Step 3: Implement**

In `register()`, after `self._guards[registration.actuator_id] = _Guard(...)`:

```python
        guard = self._guards[registration.actuator_id]
        if self._running:
            # app.py registers production actuators from the registry AFTER
            # start() has run (the spine has to be up before the registry can
            # be read). A guard created then must get the same watcher a
            # start()-time guard gets, or heartbeat loss protects nothing —
            # which is exactly what shipped until 2026-08-23.
            loop = asyncio.get_running_loop()
            guard.last_beat = loop.time()
            guard.watch_task = asyncio.create_task(
                self._watch_heartbeat(guard), name=f"hb-{guard.actuator_id}"
            )
```

Note `register()` is sync and `asyncio.get_running_loop()` requires a running loop — every production call site (`app._build_from_registry`, `register_drill_actuator` via `_amain`) is inside the loop already. Tests calling `register()` before any loop exists only do so with `self._running` False, so the branch never executes there.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest services/hardware_io/tests -v`
Expected: all PASS (including the pre-existing suite — `stop()` already cancels `watch_task` regardless of how it was spawned).

- [ ] **Step 5: Commit**

```bash
git add services/hardware_io
git commit -m "fix(hardware-io): spawn heartbeat watchers for actuators registered after start()"
```

---

### Task 2: Guard tasks survive driver failures; safe-drive retries

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/safety.py` (`_watch_heartbeat` ~277, `_runtime_deadline` ~366, new `_drive_safe_with_retry`)
- Test: same test file as Task 1

**Interfaces:**
- Produces: `InterlockSupervisor._drive_safe_with_retry(guard, reason, detail)` — awaits `_drive_safe` until it succeeds or the supervisor stops; each failure emits a `SafetyEvent(reached_safe=False)` and backs off `RETRY_BACKOFF_S` (module-level `Final = 1.0`). Both background tasks use it; neither can die to a driver exception any more.

- [ ] **Step 1: Write the failing tests**

```python
class FlakyActuator(FakeActuator):
    """drive_safe() fails N times, then succeeds."""

    def __init__(self, *args, failures: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures = failures
        self.drive_safe_calls = 0

    async def drive_safe(self) -> None:
        self.drive_safe_calls += 1
        if self.drive_safe_calls <= self.failures:
            raise OSError("transient I2C fault")
        await super().drive_safe()


async def test_runtime_trip_retries_failed_safe_drive(fake_registration):
    """Finding 2: a transient driver error during a max-runtime trip must not
    leave the actuator latched-but-energised with a dead guard task."""
    events: list[SafetyEvent] = []
    actuator = FlakyActuator("flaky", safe_level(), failures=2)
    sup = InterlockSupervisor(on_event=_collecting_sink(events))
    sup.register(fake_registration(actuator_id="flaky", max_runtime_s=0.05), actuator)
    await sup.start()
    await sup.apply(make_command("flaky", non_safe_level()))
    await asyncio.sleep(0.1)  # deadline passes; first two drives fail
    # Retry backoff is patched short in the fixture (see Step 3) so this settles fast.
    await _wait_until(lambda: actuator.is_safe(), timeout=1.0)

    assert sup.is_latched("flaky")  # the latch stands
    assert actuator.drive_safe_calls >= 3  # it retried
    failed = [e for e in events if not e.reached_safe]
    assert failed  # failures were emitted, not swallowed
    assert any(e.reason == "max_runtime_exceeded" and e.reached_safe for e in events)
    await sup.stop()


async def test_heartbeat_watcher_survives_drive_failure(fake_registration):
    """Same shape via the heartbeat path: the watcher keeps watching after a
    failed drive, and the actuator lands safe once the driver recovers."""
    actuator = FlakyActuator("flaky", safe_level(), failures=1)
    sup = InterlockSupervisor(on_event=_null_sink)
    sup.register(fake_registration(actuator_id="flaky", heartbeat_timeout_s=0.05), actuator)
    await sup.start()
    await sup.apply(make_command("flaky", non_safe_level()))
    await _wait_until(lambda: actuator.is_safe(), timeout=1.0)
    assert not sup.is_latched("flaky")  # heartbeat loss does not latch
    await sup.stop()
```

Patch `RETRY_BACKOFF_S` to ~0.02 in these tests (monkeypatch the module attribute) so they run in milliseconds.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/hardware_io/tests -k "flaky or survives" -v`
Expected: FAIL — today the first driver exception kills the task; `actuator.is_safe()` never becomes true.

- [ ] **Step 3: Implement**

```python
#: Seconds between safe-drive retries after a driver failure mid-trip. Module
#: level so tests can shorten it.
RETRY_BACKOFF_S: Final = 1.0


    async def _drive_safe_with_retry(self, guard: _Guard, reason: TripReason, detail: str) -> None:
        """Drive safe, retrying until it lands or the supervisor stops.

        A trip is the one moment this service exists for; a transient driver
        error there must surface as an event and a retry, never as a dead task
        (2026-08-23 finding 2: latched-but-energised with nobody watching).
        """
        while True:
            try:
                await self._drive_safe(guard, reason, detail)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit(
                    guard,
                    reason,
                    f"drive_safe FAILED during trip, retrying: {exc!r}",
                    reached_safe=False,
                )
                if not self._running:
                    return
                await asyncio.sleep(RETRY_BACKOFF_S)
```

In `_watch_heartbeat`, replace the `await self._drive_safe(...)` call with `await self._drive_safe_with_retry(...)`, and wrap the loop body so an unexpected exception logs-and-continues rather than killing the watcher. In `_runtime_deadline`, replace its `await self._drive_safe(...)` with the retry variant (the `latched=True` assignment before it stands — refusing commands while retrying is correct).

`_emit` awaits the app's event sink, which app.py already guards top-to-bottom (`_on_safety_event` wraps everything in try/except) — but guard the emit inside `_drive_safe_with_retry`'s except branch with `contextlib.suppress(Exception)` anyway: an event sink failure must not break the retry loop that is the actual safety mechanism.

- [ ] **Step 4: Run the full hardware-io suite + types**

Run: `uv run pytest services/hardware_io/tests -v && uv run mypy services/hardware_io`
Expected: all PASS, mypy clean.

- [ ] **Step 5: Commit**

```bash
git add services/hardware_io
git commit -m "fix(hardware-io): retry failed safe-drives during trips instead of dying silently"
```

---

### Task 3: hardware-io subscribes to the engine's heartbeat

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/spine.py` (new `subscribe_heartbeats`, after `watch_assignments` ~line 315)
- Modify: `services/hardware_io/bellasreef_hardware_io/app.py` (`_connect_spine`, after `CommandConsumer.subscribe()` ~line 646)
- Test: `services/hardware_io/tests/test_spine.py` (follow its existing fake-NATS pattern)

**Interfaces:**
- Consumes: `subjects.heartbeat("control-engine")`, `Heartbeat` model, `InterlockSupervisor.heartbeat()` (sync).
- Produces: `Spine.subscribe_heartbeats(component: str, on_beat: Callable[[], None]) -> None` — core subscription; validates the payload as `Heartbeat`, drops malformed ones with a warning, calls `on_beat()` per valid beat.

- [ ] **Step 1: Write the failing test**

```python
async def test_heartbeat_subscription_feeds_supervisor(fake_nats_spine):
    beats: list[int] = []
    spine = fake_nats_spine
    await spine.subscribe_heartbeats("control-engine", lambda: beats.append(1))

    beat = Heartbeat(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="control-engine",
        component="control-engine",
        sequence=1,
        interval_s=1.0,
    )
    await deliver(spine, subjects.heartbeat("control-engine"), beat.model_dump_json().encode())
    assert beats == [1]

    await deliver(spine, subjects.heartbeat("control-engine"), b"not json")
    assert beats == [1]  # malformed beat dropped, subscription alive

    await deliver(
        spine,
        subjects.heartbeat("control-engine"),
        beat.model_copy(update={"sequence": 2}).model_dump_json().encode(),
    )
    assert beats == [1, 1]
```

Use `test_spine.py`'s existing helpers for constructing the spine against a fake/loopback NATS and for delivering messages — mirror however `watch_assignments` is tested there.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest services/hardware_io/tests/test_spine.py -k heartbeat -v`
Expected: FAIL with `AttributeError: 'Spine' object has no attribute 'subscribe_heartbeats'`.

- [ ] **Step 3: Implement in spine.py**

```python
    async def subscribe_heartbeats(self, component: str, on_beat: Callable[[], None]) -> None:
        """Core subscription to one component's liveness beacon.

        Core pub/sub on purpose, like the publish side: a replayed heartbeat
        would make a dead controller look alive. The callback is synchronous
        and cheap (InterlockSupervisor.heartbeat stamps a monotonic time) —
        nothing here may block the NATS client's task. Malformed payloads are
        dropped with a warning, same contract as watch_assignments: parsing is
        guarded so a bad message cannot kill the subscription.
        """
        if self._nc is None:
            raise RuntimeError("spine not connected")

        async def _cb(msg: Msg) -> None:
            try:
                Heartbeat.model_validate_json(msg.data)
            except ValidationError:
                log.warning("dropping an undecodable heartbeat", extra={"subject": msg.subject})
                return
            on_beat()

        await self._nc.subscribe(subjects.heartbeat(component), cb=_cb)
        log.info("watching heartbeats", extra={"component": component})
```

In `app.py::_connect_spine`, after `await self.commands.subscribe()`:

```python
        # The controller's liveness beacon. Subscribed last, after every
        # actuator is registered and watched: a beat that arrives before the
        # watchers exist would be stamped and forgotten, and one that arrives
        # after is exactly what resets their deadlines.
        await self.spine.subscribe_heartbeats("control-engine", self.supervisor.heartbeat)
```

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/hardware_io/tests -v && uv run mypy services/hardware_io`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/hardware_io
git commit -m "feat(hardware-io): subscribe to the control-engine heartbeat and feed the interlock supervisor"
```

---

### Task 4: control-engine publishes its heartbeat from the loop

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`_loop` ~line 235)
- Test: `services/control_engine/tests/test_app.py`

**Interfaces:**
- Consumes: `CommandPublisher.heartbeat(interval_s)` (exists, line ~316; raises `RuntimeError` when not connected).
- Produces: one heartbeat per loop iteration while the clock is trusted and the publisher exists; **no heartbeat when the clock is untrusted** — that silence is the signal hardware-io acts on (module docstring already promises exactly this).

- [ ] **Step 1: Write the failing tests**

```python
async def test_loop_publishes_heartbeat_each_tick(engine_with_fake_publisher):
    engine, publisher = engine_with_fake_publisher  # follow test_app.py's fixture idiom
    await run_loop_iterations(engine, 3)
    assert publisher.heartbeats == 3  # add a counter to the fake


async def test_no_heartbeat_while_clock_untrusted(engine_with_fake_publisher, monkeypatch):
    engine, publisher = engine_with_fake_publisher
    force_clock_untrusted(engine, monkeypatch)  # existing tests flip clock trust; reuse that
    await run_loop_iterations(engine, 3)
    assert publisher.heartbeats == 0


async def test_heartbeat_publish_failure_does_not_kill_the_loop(engine_with_fake_publisher):
    engine, publisher = engine_with_fake_publisher
    publisher.fail_heartbeats = True
    await run_loop_iterations(engine, 2)  # would raise out of _loop before the fix
    assert engine_still_ticking(engine)
```

`test_app.py` already has machinery for driving `_loop`/`_tick` with fake publishers and for toggling clock trust — reuse it; do not build a parallel harness.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/control_engine/tests/test_app.py -k heartbeat -v`
Expected: FAIL (zero heartbeats published).

- [ ] **Step 3: Implement**

In `_loop`, after `self._refresh_clock_trust()` and before the assignments-loaded block:

```python
            if self.publisher is not None and self._clock_trusted:
                # The liveness beacon hardware-io's interlocks watch. Silence
                # is deliberate in exactly two cases: the clock is untrusted
                # (we cannot honestly timestamp "alive now" — see the module
                # docstring), or this loop has stalled and stopped iterating.
                # Both are cases where hardware-io driving actuators to safe
                # state is the designed response, so a failed publish is
                # logged and NOT retried here — the next iteration beats
                # again, and hardware-io's timeout absorbs a transient gap.
                try:
                    await self.publisher.heartbeat(self._loop_interval_s)
                except Exception:
                    log.warning("heartbeat publish failed", exc_info=True)
```

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/control_engine/tests -v && uv run mypy services/control_engine`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/control_engine
git commit -m "feat(control-engine): publish the heartbeat the interlock supervisor has been waiting for"
```

---

### Task 5: engine forgets a channel when hardware-io reports it autonomously safe

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/publisher.py` (new `subscribe_states`, modeled on `subscribe_assignments` ~line 288)
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`run()` wiring ~line 188; new `_on_actuator_state`)
- Test: `services/control_engine/tests/test_app.py`, `services/control_engine/tests/test_publisher.py`

**Interfaces:**
- Consumes: `ActuatorState` (contracts), `subjects.ALL_STATE`, `LightingScheduler.forget(channel_id)`.
- Produces: `CommandPublisher.subscribe_states(handler: Callable[[ActuatorState], None]) -> None` (core subscription — a JetStream publish traverses core subjects too, same as assignments); `ControlEngine._on_actuator_state(state)` — calls `scheduler.forget` for autonomous transitions.

- [ ] **Step 1: Write the failing tests**

```python
def test_autonomous_safe_state_forgets_the_channel(engine):
    """Finding 6: hardware-io restarts rebuild dark, but the engine's memory
    said 0.8 — the 300s refresh then snapped a dark channel to curve duty in
    one step, bypassing the slew. An autonomous state from hardware-io means
    the scheduler's memory is no longer true."""
    prime_scheduler_memory(engine, "pca9685-0", duty=0.8)
    engine._on_actuator_state(make_state("pca9685-0", reason="safe_state", source="hardware-io"))
    assert "pca9685-0" not in engine.scheduler._last_duty  # cold again; next intent is "initial"


def test_startup_and_latch_states_also_forget(engine):
    for reason in ("startup", "interlock_latch"):
        prime_scheduler_memory(engine, "ch", duty=0.5)
        engine._on_actuator_state(make_state("ch", reason=reason, source="hardware-io"))
        assert "ch" not in engine.scheduler._last_duty


def test_commanded_state_does_not_forget(engine):
    """'commanded' states are echoes of our own publishes — forgetting on them
    would cold-start every channel on every command and defeat the deadband."""
    prime_scheduler_memory(engine, "ch", duty=0.5)
    engine._on_actuator_state(make_state("ch", reason="commanded", source="hardware-io"))
    assert engine.scheduler._last_duty.get("ch") == 0.5
```

Plus one `test_publisher.py` test that `subscribe_states` validates/drops malformed payloads and survives a raising handler — copy the `subscribe_assignments` test shape exactly.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/control_engine/tests -k "forget or subscribe_states" -v`
Expected: FAIL (`_on_actuator_state` does not exist).

- [ ] **Step 3: Implement**

`publisher.py` — copy `subscribe_assignments` verbatim, swapping the model and subject:

```python
    async def subscribe_states(self, handler: Callable[[ActuatorState], None]) -> None:
        """Live actuator-state traffic, on core pub/sub.

        Same transport note as subscribe_assignments: a JetStream publish
        traverses core subjects too, so this hears every state hardware-io
        publishes without a durable to leak. Malformed payloads are dropped
        with a log; parsing and handling are guarded separately so a handler
        that raises cannot kill the subscription silently.
        """
        if self._nc is None:
            raise RuntimeError("publisher not connected")

        async def _on_message(msg: Msg) -> None:
            try:
                state = ActuatorState.model_validate_json(msg.data)
            except ValidationError:
                log.warning("dropping an undecodable actuator state", extra={"subject": msg.subject})
                return
            try:
                handler(state)
            except Exception:  # broad by design - see docstring
                log.exception("state handling failed", extra={"subject": msg.subject})

        await self._nc.subscribe(subjects.ALL_STATE, cb=_on_message)
        log.info("subscribed to actuator state", extra={"subject": subjects.ALL_STATE})
```

(Import `ActuatorState` from `bellasreef_contracts` at the top of publisher.py.)

`app.py`:

```python
#: State reasons that mean hardware-io moved the actuator on its own —
#: the scheduler's emission memory for that channel is no longer true.
#: "commanded" is excluded: those are echoes of our own publishes, and
#: forgetting on them would cold-start every channel on every command.
_AUTONOMOUS_STATE_REASONS: Final[frozenset[str]] = frozenset(
    {"startup", "safe_state", "interlock_latch"}
)


def _on_actuator_state(self, state: ActuatorState) -> None:
    if state.reason not in self._AUTONOMOUS_STATE_REASONS:
        return
    self.scheduler.forget(state.actuator_id)
    log.info(
        "hardware reported an autonomous transition; scheduler memory cleared",
        extra={"actuator_id": state.actuator_id, "reason": state.reason},
    )
```

Wire in `run()` next to `subscribe_assignments`:

```python
            await self.publisher.subscribe_states(self._on_actuator_state)
```

Check the actual field name for the reason on `ActuatorState` in `contracts/python/bellasreef_contracts` (it is `reason: StateReason`) and the exact `StateReason` literals before writing the frozenset — the three above are what hardware-io's `_TRIP_STATE_REASON` and `_publish_startup_states` emit; verify against the contract and adjust if the literal spellings differ.

- [ ] **Step 4: Run both service suites + types**

Run: `uv run pytest services/control_engine/tests -v && uv run mypy services/control_engine`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/control_engine
git commit -m "fix(control-engine): forget scheduler memory when hardware-io reports an autonomous safe state"
```

---

### Task 6: drill honesty — the false green closes

**Files:**
- Modify: `services/hardware_io/tests/test_drills.py` (add the registry-path drill assertion)

**Interfaces:**
- Consumes: everything Tasks 1–3 produced.

- [ ] **Step 1: Write the test (should pass already if Tasks 1–3 are correct — it is a regression fence, and the point is that it FAILS on main-before-this-branch)**

```python
async def test_heartbeat_drill_covers_registry_built_actuators(fake_registration, fake_actuator):
    """The 2026-08-23 review found the drills passing while the protection was
    absent: the drill dummy registers before start() and got a watcher, while
    every registry-built actuator registers after and got none. This drill
    replicates the production ordering exactly — start() first, register()
    after, beats via the same path the spine callback uses — and asserts the
    trip AND the recovery contract (safe until explicitly commanded again)."""
    events: list[SafetyEvent] = []
    sup = InterlockSupervisor(on_event=_collecting_sink(events))
    await sup.start()  # production ordering
    sup.register(fake_registration(actuator_id="prod", heartbeat_timeout_s=0.05), fake_actuator)

    sup.heartbeat()  # engine alive
    await sup.apply(make_command("prod", non_safe_level()))
    assert not fake_actuator.is_safe()

    await asyncio.sleep(0.15)  # engine dies: no beats
    assert fake_actuator.is_safe()  # tripped dark
    assert not sup.is_latched("prod")  # heartbeat loss never latches

    sup.heartbeat()  # engine returns
    await asyncio.sleep(0.1)
    assert fake_actuator.is_safe()  # did NOT spring back on

    assert await sup.apply(make_command("prod", non_safe_level())) == "applied"
    assert not fake_actuator.is_safe()  # explicit command restores
```

- [ ] **Step 2: Run it; verify it passes on the branch and (mentally) fails on main**

Run: `uv run pytest services/hardware_io/tests/test_drills.py -v`
Expected: PASS. Sanity: `git stash && uv run pytest services/hardware_io/tests/test_drills.py -k registry -v; git stash pop` — expect FAIL on the stash (no watcher).

- [ ] **Step 3: Commit**

```bash
git add services/hardware_io/tests/test_drills.py
git commit -m "test(hardware-io): drill the registry-built actuator path the false green never covered"
```

---

### Task 7: full check, PR, merge, deploy, hardware drills

- [ ] **Step 1:** `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` — everything green locally (env skips declared).
- [ ] **Step 2:** Push branch, open PR titled `fix(safety): make heartbeat-loss safe-state real end-to-end`. PR body: the three links, why they ship together, the deploy-ordering note from "Context" above, and the drill plan. Wait for CI green. Merge (squash per repo convention — check `gh pr list --state merged --limit 3 --json mergeCommit,title` for the pattern used).
- [ ] **Step 3:** Deploy: `scripts/deploy-pi.sh` from a clean, pushed main. Verify its telemetry gate passes.
- [ ] **Step 4: Engine-kill drill (pre-approved).** On the Pi: confirm the schedule holds `pca9685-0` at a nonzero duty (engine logs). Then stop the engine **from inside the container** (the `docker kill` trap — CLAUDE.md): `ssh reef 'docker exec bellasreef-control-engine-1 python -c "import os,signal; os.kill(1, signal.SIGTERM)"'`. Within ~31 s hardware-io must log `safety event ... reason=heartbeat_timeout` for `pca9685-0` (and `pi-pwm-0`) and publish `ActuatorState reason=safe_state duty=0`. The container's restart policy brings the engine back; beats resume; the engine's state subscription has forgotten the channel, so recovery must log an `initial`/`converge` sequence slewing up from 0 — **not** a single command at curve duty. Record timestamps and log lines.
- [ ] **Step 5: NATS-outage drill — SKIP for now**; run it after batch C deploys (without finding 8's fix the engine crash-loops through the outage, which muddies what this drill measures). Note this in the report.
- [ ] **Step 6:** Update CLAUDE.md is NOT done autonomously; instead record drill results in the session report for David, including exact meter-free evidence (log lines, wire states). Bench boundary: no electrical claims — duty 0 = dark was proven by David's meter on 2026-08-15/17 and is cited, not re-derived.

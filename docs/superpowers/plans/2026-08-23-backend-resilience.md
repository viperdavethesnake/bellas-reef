# Backend Resilience Cluster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A transient fault degrades a service instead of killing it — a chip off the bus, a Postgres blip, or a NATS restart must not crash-loop hardware-io or the engine — plus three correctness quickies: identity-resolved PWM chip in the factory, honest `bind_device` conflicts, and the API's missing metrics endpoint.

**Architecture:** Four resilience fixes from the 2026-08-23 review (findings 4, 5, 7, 8) share one theme: guarded paths exist inches away from each unguarded one, proving the omissions are oversights, not policy. Clock trust (finding 4) additionally hides a configuration truth: `BELLASREEF_ASSUME_CLOCK_TRUSTED: "1"` in `deploy/compose.yaml` means clock trust is a **constant** in production — so the fix is re-evaluation machinery plus a real kernel oracle (`adjtimex`) run in **shadow mode** (logged, not enforced) until David rules on flipping the env var.

**Tech Stack:** Python 3.13, asyncio, nats-py 2.15 (`Client.is_connected`), SQLAlchemy async, ctypes (`adjtimex`), prometheus_client, pytest.

**Spec:** 2026-08-23 `services/` code review, findings 3, 4, 5, 7, 8, 9 + the API-metrics day-1 requirement in CLAUDE.md ("Every service: healthcheck endpoint, structured JSON logs, metrics endpoint").

## Global Constraints

- Python 3.13+, fully typed, `mypy --strict` clean. Ruff for lint/format.
- Conventional commits; feature branch; PR; CI green before merge.
- Local gate: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh`.
- **No production behavior change to clock-trust enforcement in this plan.** The env override keeps winning exactly as configured; the new oracle only logs. Flipping enforcement is David's ruling, listed in the PR body.
- Deploy is part of done: CI green → `scripts/deploy-pi.sh` → telemetry verified on the wire. The NATS-outage drill deferred from batch A runs after this deploys.

## Context a fresh engineer needs

- **Finding 5** — `services/hardware_io/bellasreef_hardware_io/spine.py::CommandConsumer.drain_once` (~line 446): `supervisor.apply(command)` (485) and the `_audit(...)` calls (479, 493) are unguarded; an exception unwinds to process exit before ack/term, and the un-acked workqueue message redelivers into the restarted service (`max_deliver=3`). The state publishes next door (`_publish_applied_state`, 585) are deliberately wrapped — copy that stance.
- **Finding 7** — `services/control_engine/bellasreef_control_engine/app.py::_tick` (~532): `_expire_overrides` (413: `self.overrides.release(...)`) and `_reload_overrides` (484: `list_active()`) await Postgres bare; `_reload_schedules` (493) is wrapped with a keep-last-good + once-per-outage warn pattern — copy it exactly (`_override_read_failing` flag, `metrics` counter).
- **Finding 8** — `services/control_engine/bellasreef_control_engine/publisher.py::connected` (~131) is `self._js is not None`; nats-py exposes `Client.is_connected`. Also `ControlEngine._publish` (~588) awaits `publisher.emit` bare inside `_tick`; a PubAck timeout kills the engine.
- **Finding 4** — hardware-io `app.py`: `self._clock_trusted = clock_is_trusted()` once in `__init__` (~183); `_refresh_clock_trust` (~826) only re-exports the frozen value; `InterlockSupervisor` gets `clock_trusted` at construction with no setter (safety.py ~165). The engine re-checks every loop via `bellasreef_service.clock.clock_is_trusted` — a **blocking subprocess on the event loop every second** (cleanup finding). hardware-io duplicates the function in its own app.py (~75). Consolidate into `services/shared/bellasreef_service/clock.py`.
- **Finding 3** — `services/hardware_io/bellasreef_hardware_io/factory.py` (~117): `PiPwmChannel(int(binding["channel"]), assignment.device_id, sysfs=sysfs)` — no `chip_root`, so `pipwm.py`'s `PWM_CHIP_ROOT = Path("/sys/class/pwm/pwmchip0")` default applies. `capabilities.find_pwm_chip()` (capabilities.py:73) already resolves by identity (`RP1_PWM0_DEVICE = "1f00098000.pwm"`). Spec commit dd6a68b ruled this; discovery complies, the factory does not.
- **Finding 9** — `services/api/bellasreef_api/store.py::bind_device` (~370): the create path INSERTs `ON CONFLICT (device_id) DO NOTHING` (442) and never checks rowcount; the endpoint's 409 (app.py ~1484) checks only the *channel's* holder via `device_bound_to`, so a device_id already bound to a **different channel** returns `created=True` while writing nothing, then publishes a DeviceAssignment that contradicts Postgres.
- **Metrics** — the API exposes only `/healthz` (app.py:927). hardware-io and the engine use `bellasreef_service.httpd.MetricsServer(probe=..., registry=..., port=...)` started in their run loops; the API should start one in its FastAPI lifespan on port 9103 (`BELLASREEF_METRICS_PORT`), keeping the pattern identical across services rather than adding an authenticated FastAPI route.
- Test conventions: `services/*/tests/`, pytest, existing fakes. API tests use its own fixtures (`services/api/tests/`) — find the lifespan/background-component harness in `test_background_components.py` and follow it for the MetricsServer wiring test.

---

### Task 1: BR_CMD drain path survives driver and audit failures

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/spine.py` (`CommandConsumer.drain_once` ~446, `_audit` ~582)
- Test: `services/hardware_io/tests/test_spine.py`

**Interfaces:**
- Produces: unchanged signatures. New behavior: a `supervisor.apply` exception naks the message with a 1 s delay (redelivery until `max_deliver`, then the broker stops; the 30 s command TTL means later redeliveries are refused as expired and terminated — no poison message survives); an audit-publish failure is logged and swallowed (the ack/term still happens).

- [ ] **Step 1: Write the failing tests**

```python
async def test_driver_failure_naks_instead_of_killing(command_consumer_harness):
    """Finding 5: the PCA9685 physically dropped off bus 1 on 2026-08-15. A
    driver OSError mid-apply must nak the message, not unwind the process —
    the un-acked workqueue message was redelivering into every restart."""
    harness = command_consumer_harness
    harness.supervisor.fail_apply_with(OSError("Remote I/O error"))
    msg = harness.enqueue(valid_command("pca9685-0"))
    outcomes = await harness.consumer.drain_once()
    assert outcomes == []  # nothing applied, nothing raised
    assert msg.naked_with_delay == 1.0
    assert not msg.acked and not msg.termed


async def test_audit_publish_failure_does_not_lose_the_term(command_consumer_harness):
    """A broker blip during a refusal's audit publish must not convert a
    routine refusal into a service restart. The term still lands."""
    harness = command_consumer_harness
    harness.spine.fail_audit_publishes()
    msg = harness.enqueue(expired_command("pca9685-0"))
    outcomes = await harness.consumer.drain_once()
    assert outcomes == ["rejected_expired"]
    assert msg.termed
```

Build on `test_spine.py`'s existing fake spine/consumer harness (it already fakes fetch/ack/term for the refusal-path tests); extend the fake `Msg` with `nak(delay=...)` recording.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/hardware_io/tests/test_spine.py -k "naks or lose_the_term" -v`
Expected: first test errors with the raised `OSError` escaping `drain_once`.

- [ ] **Step 3: Implement**

In `drain_once`, wrap the apply:

```python
            try:
                outcome = await self._supervisor.apply(command)
            except Exception:
                # A driver fault (chip off the bus, transient I2C error) is a
                # hardware event, not a reason to unwind the process — that
                # turned one off-bus chip into a crash-loop, because the
                # un-acked workqueue message redelivered into every restart.
                # Nak with a delay: a transient fault succeeds on redelivery;
                # a persistent one burns max_deliver and then the command's
                # own 30s TTL refuses successors as expired.
                log.critical(
                    "driver failed applying a command; message nak'd for redelivery",
                    extra={"actuator_id": command.actuator_id},
                    exc_info=True,
                )
                await msg.nak(delay=1.0)
                continue
```

Wrap each `self._audit(...)` call site (malformed and refused paths) — or, simpler and equivalent, make `_audit` itself swallow:

```python
    async def _audit(self, event_type: str, detail: dict[str, object]) -> None:
        """Best-effort: an audit publish failure is logged, never raised.

        The ack/term deciding the message's fate must happen regardless — a
        JetStream hiccup on the audit stream was escaping drain_once and
        exiting the process mid-refusal (2026-08-23 finding 5), while the
        state publishes next door were already wrapped."""
        try:
            await self._spine.publish_audit("command", {"event": event_type, **detail})
        except Exception:
            log.warning(
                "audit publish failed; event dropped",
                extra={"event": event_type},
                exc_info=True,
            )
```

Also wrap the `await msg.term()` / `await msg.ack()` calls? No — leave them bare: an ack/term failure means the broker connection is gone, and crashing to the drilled restart path is then correct. Note this in a comment only if a reviewer asks; do not add the comment pre-emptively.

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/hardware_io/tests -v && uv run mypy services/hardware_io`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/hardware_io
git commit -m "fix(hardware-io): a driver or audit failure in the command drain degrades instead of crash-looping"
```

---

### Task 2: engine ticks survive Postgres blips

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`_expire_overrides` ~393, `_reload_overrides` ~451; add `_override_read_failing` state + metrics counter in `_Metrics`)
- Test: `services/control_engine/tests/test_overrides.py` or `test_app.py` (wherever the existing override-tick tests live)

**Interfaces:**
- Produces: unchanged signatures; both methods now catch `Exception`, keep the last good `self._held`, count `self.metrics.override_io_errors`, and warn once per outage (mirroring `_reload_schedules`' `_schedule_read_failing` idiom line-for-line).

- [ ] **Step 1: Write the failing test**

```python
async def test_tick_survives_override_store_outage(engine_with_flaky_store):
    """Finding 7: one dropped Postgres connection in list_active() unwound
    _tick -> _loop -> run() and crash-looped the engine, stripping all
    lighting control for the length of the flap — while _reload_schedules
    next door was wrapped precisely against this."""
    engine, store = engine_with_flaky_store
    store.fail_next("list_active", ConnectionError("server closed the connection"))
    await engine._tick(datetime.now(UTC))  # must not raise
    store.fail_next("release", ConnectionError("server closed the connection"))
    prime_expired_hold(engine, "pca9685-0")
    await engine._tick(datetime.now(UTC))  # must not raise; hold stays held for retry
    assert "pca9685-0" in engine._held  # not silently dropped on a failed release
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest services/control_engine/tests -k outage -v`
Expected: FAIL with the ConnectionError escaping `_tick`.

- [ ] **Step 3: Implement**

Add to `_Metrics`:

```python
        self.override_io_errors = Counter(
            "bellasreef_override_io_errors_total",
            "Override store read/write failures absorbed by the tick loop",
            registry=registry,
        )
```

Wrap `_reload_overrides`' body (the store read plus the rebuild) and `_expire_overrides`' per-override release in the `_reload_schedules` pattern: `self._override_read_failing` bool, one `log.warning(..., exc_info=True)` per outage, counter increment, keep `self._held` as-is on failure. In `_expire_overrides`, a failed `release()` keeps the entry in `self._held` so the next tick retries (the monotonic deadline still marks it expired; do NOT delete before the release lands, or the row leaks open in Postgres with nobody retrying).

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/control_engine/tests -v && uv run mypy services/control_engine`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/control_engine
git commit -m "fix(control-engine): a flapping database no longer kills the tick loop via the override paths"
```

---

### Task 3: honest `connected`, guarded publishes — a broker blip degrades the engine

**Files:**
- Modify: `services/control_engine/bellasreef_control_engine/publisher.py` (`connected` ~131)
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`_publish` ~588)
- Test: `services/control_engine/tests/test_publisher.py`, `test_app.py`

**Interfaces:**
- Produces: `CommandPublisher.connected` → `self._nc is not None and self._nc.is_connected` (nats-py maintains `is_connected` across RECONNECTING); `ControlEngine._publish` catches publish exceptions, counts `suppressed{cause="publish_failed"}`, and does NOT `mark_emitted` (the existing comment already demands that ordering).

- [ ] **Step 1: Write the failing tests**

```python
def test_connected_reflects_the_client_not_the_handle(publisher_with_fake_nc):
    publisher, nc = publisher_with_fake_nc
    nc.is_connected = False  # RECONNECTING: _js is still non-None
    assert publisher.connected is False


async def test_publish_failure_suppresses_instead_of_killing(engine_with_fake_publisher):
    """Finding 8: a PubAck timeout during a NATS restart unwound _tick and
    crash-looped the engine; health reported 'spine ok' the whole window."""
    engine, publisher = engine_with_fake_publisher
    publisher.fail_emits_with(TimeoutError("nats: request timeout"))
    prime_due_intent(engine, "pca9685-0", duty=0.5)
    await engine._tick(datetime.now(UTC))  # must not raise
    assert scheduler_has_no_emission_recorded(engine, "pca9685-0")  # mark_emitted skipped
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/control_engine/tests -k "reflects or suppresses" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
    @property
    def connected(self) -> bool:
        """The client's word, not the handle's. `_js is not None` stayed True
        through every RECONNECTING window, so the no_spine suppression gate
        waved publishes into a dead broker and the PubAck timeout killed the
        engine (2026-08-23 finding 8)."""
        return self._nc is not None and self._nc.is_connected
```

In `_publish`:

```python
        try:
            await self.publisher.emit(command)
        except Exception:
            # A broker blip mid-publish. Suppress and let the next tick retry:
            # mark_emitted must not run (recording an emission the broker never
            # accepted would make the scheduler skip the next one), and the
            # engine staying alive is what lets the retry exist.
            self.metrics.suppressed.labels("publish_failed").inc()
            log.warning(
                "command publish failed; will retry next tick",
                extra={"actuator_id": intent.channel_id},
                exc_info=True,
            )
            return
```

Check `health()` (app.py ~645): it already consults `publisher.connected`, so the honest property fixes the lying health endpoint for free — verify the existing health test still passes and add an assertion that a disconnected client yields `"spine not connected"`.

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/control_engine/tests -v && uv run mypy services/control_engine`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/control_engine
git commit -m "fix(control-engine): a NATS outage suppresses publishes instead of crash-looping, and health stops lying about it"
```

---

### Task 4: clock trust — re-evaluated, consolidated, with a real kernel oracle in shadow mode

**Files:**
- Modify: `services/shared/bellasreef_service/clock.py` (add `adjtimex` oracle + async cadence helper)
- Modify: `services/hardware_io/bellasreef_hardware_io/app.py` (delete its duplicate `clock_is_trusted`, import the shared one; make `_refresh_clock_trust` actually refresh, off-loop, on a 30 s cadence)
- Modify: `services/hardware_io/bellasreef_hardware_io/safety.py` (`InterlockSupervisor.set_clock_trusted`)
- Modify: `services/control_engine/bellasreef_control_engine/app.py` (`_refresh_clock_trust` → same off-loop cadence helper; kills the blocking 1 Hz subprocess)
- Test: `services/shared` tests (find them: `ls services/shared`; if none exist, `services/hardware_io/tests/test_clock.py` covers the shared module), plus supervisor setter test

**Interfaces:**
- Produces:
  - `bellasreef_service.clock.kernel_clock_synchronised() -> bool | None` — ctypes `adjtimex(2)`; returns None when the syscall is unavailable (non-Linux dev Mac), True/False from `ret != TIME_ERROR` otherwise.
  - `bellasreef_service.clock.clock_is_trusted() -> bool` — unchanged contract (timedatectl → env fallback), now also **logging** the kernel oracle's answer when it disagrees with what is returned (shadow mode).
  - `bellasreef_service.clock.ClockTrust` — small helper: `refresh_due(now_monotonic) -> bool` on a 30 s cadence and `async evaluate() -> bool` running `clock_is_trusted` via `asyncio.to_thread`.
  - `InterlockSupervisor.set_clock_trusted(value: bool) -> None`.
- Consumes: `os.environ["BELLASREEF_ASSUME_CLOCK_TRUSTED"]` — behavior unchanged where set (which is production, all three services).

- [ ] **Step 1: Write the failing tests**

```python
def test_supervisor_clock_trust_is_settable():
    sup = InterlockSupervisor(on_event=_null_sink, clock_trusted=False)
    sup.set_clock_trusted(True)
    # a fresh command is no longer rejected_clock — reuse the existing
    # rejected_clock test's fixtures inverted


def test_kernel_oracle_returns_none_where_unavailable(monkeypatch):
    """On the dev Mac there is no adjtimex; the oracle must say 'unknown',
    never guess."""
    monkeypatch.setattr(clock, "_libc", None)
    assert clock.kernel_clock_synchronised() is None


async def test_hardware_io_refreshes_clock_trust(monkeypatch):
    """Finding 4: trust was evaluated once at __init__ and frozen — a power
    cut left every command rejected_clock until a manual restart. The loop
    must pick up a change within one refresh interval."""
    service = HardwareIO()
    answers = iter([False, True])
    monkeypatch.setattr(app_module, "clock_is_trusted", lambda: next(answers))
    force_clock_refresh_due(service)
    await service._refresh_clock_trust_async()
    assert service._clock_trusted is False
    force_clock_refresh_due(service)
    await service._refresh_clock_trust_async()
    assert service._clock_trusted is True
    assert service.supervisor._clock_trusted is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/hardware_io/tests -k clock -v`
Expected: FAIL (`set_clock_trusted` missing, refresh method missing).

- [ ] **Step 3: Implement**

`clock.py` — the oracle (aarch64 and x86_64 share the layout; `long` is 8 bytes on both):

```python
import ctypes
import ctypes.util

_TIME_ERROR = 5  # TIME_ERROR / TIME_BAD: clock not synchronised


class _Timex(ctypes.Structure):
    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_pad", ctypes.c_int * 11),
    ]


def _load_libc() -> ctypes.CDLL | None:
    try:
        name = ctypes.util.find_library("c")
        return ctypes.CDLL(name, use_errno=True) if name else None
    except OSError:
        return None


_libc = _load_libc()


def kernel_clock_synchronised() -> bool | None:
    """Ask the kernel, not systemd: adjtimex(2) is visible from inside a
    container, where timedatectl is not. Returns None where the syscall is
    unavailable (dev Mac) or fails — unknown is an answer, a guess is not."""
    if _libc is None or not hasattr(_libc, "adjtimex"):
        return None
    buf = _Timex()
    buf.modes = 0  # read-only query
    try:
        ret = _libc.adjtimex(ctypes.byref(buf))
    except Exception:
        return None
    if ret < 0:
        return None
    return ret != _TIME_ERROR
```

In `clock_is_trusted()`, before returning the env-override answer, log the shadow comparison exactly once per disagreement flip (module-level `_last_shadow: bool | None` latch):

```python
    kernel = kernel_clock_synchronised()
    if kernel is not None and kernel != result:
        _log_shadow_disagreement(kernel=kernel, returned=result)
```

(`_log_shadow_disagreement` logs at WARNING with `extra={"event": "clock_shadow_disagreement", "kernel": kernel, "returned": returned}` and latches so it fires on flips, not every call.)

`safety.py`:

```python
    def set_clock_trusted(self, value: bool) -> None:
        """The host clock's trust changes at runtime on an RTC-less board —
        chrony steps it good after a power cut, and freezing the __init__
        answer left every command rejected_clock until a manual restart
        (2026-08-23 finding 4)."""
        self._clock_trusted = value
```

hardware-io `app.py`: delete the local `clock_is_trusted` (keep the name importable — `from bellasreef_service.clock import clock_is_trusted` — because `__all__` exports it and tests import it); add `self._clock_refresh_due = 0.0` and:

```python
_CLOCK_REFRESH_S: Final = 30.0


async def _refresh_clock_trust_async(self) -> None:
    now = time.monotonic()
    if now < self._clock_refresh_due:
        self.metrics.clock_trusted.set(1.0 if self._clock_trusted else 0.0)
        return
    self._clock_refresh_due = now + self._CLOCK_REFRESH_S
    trusted = await asyncio.to_thread(clock_is_trusted)
    if trusted != self._clock_trusted:
        log.warning("clock trust changed", extra={"clock_trusted": trusted, "event": "clock_trust"})
        self._clock_trusted = trusted
        self.supervisor.set_clock_trusted(trusted)
    self.metrics.clock_trusted.set(1.0 if self._clock_trusted else 0.0)
```

Call it from `_loop` in place of the sync `_refresh_clock_trust()` (keep the old name delegating or update the call site — pick whichever keeps the diff smaller; the health() read of `self._clock_trusted` is unchanged). Engine `app.py`: same cadence treatment — `_refresh_clock_trust` gains the 30 s gate and `asyncio.to_thread`, killing the 1 Hz blocking subprocess on the event loop (the engine's *reaction* to a flip — scheduler reset, heartbeat silence — is already written and stays).

Engine note: with a 30 s cadence the "suspend heartbeats on untrusted clock" reaction can lag a flip by up to 30 s. That is inside the design's tolerance (hardware-io's own guards carry 30 s timeouts) — say so in a comment where the cadence constant is declared.

- [ ] **Step 4: Run all three suites + types**

Run: `uv run pytest services -v && uv run mypy services`
Expected: PASS (adjust to the repo's actual check invocations in `scripts/check.sh` if narrower).

- [ ] **Step 5: Commit**

```bash
git add services
git commit -m "fix(clock): trust is re-evaluated at runtime, off-loop, with a kernel oracle in shadow mode"
```

**PR-body note (verbatim, for David):** production compose sets `BELLASREEF_ASSUME_CLOCK_TRUSTED: "1"` for all three services, so clock trust is currently a constant — the RTC-less power-cut protection is configured off. The `adjtimex` oracle now logs what the kernel actually says. After a deploy plus a few days of clean shadow logs, removing the env var from `deploy/compose.yaml` makes the protection real; that flip is yours to rule on.

---

### Task 5: factory resolves the RP1 PWM chip by identity

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/factory.py` (`build_from_assignments` signature + pi-pwm branch ~117)
- Modify: `services/hardware_io/bellasreef_hardware_io/app.py` (`_build_from_registry` call ~231)
- Test: `services/hardware_io/tests/test_factory.py`

**Interfaces:**
- Consumes: `capabilities.find_pwm_chip() -> Path | None` (identity-resolved, `RP1_PWM0_DEVICE = "1f00098000.pwm"`).
- Produces: `build_from_assignments(..., pwm_chip_root: Path | None = None)` — resolved once per build via `find_pwm_chip()` when not injected; a pi-pwm assignment on a host with no identity-resolved chip is skipped with an error log (same contract as any unbuildable assignment), never built on a guessed index.

- [ ] **Step 1: Write the failing tests**

```python
def test_pipwm_built_on_the_identity_resolved_chip(tmp_path):
    """Finding 3 / spec dd6a68b: the pwmchipN index moves between kernels; a
    fan-header block renumbered to pwmchip0 would take lighting duty commands
    with every software check green. The factory must use find_pwm_chip's
    answer, not the pwmchip0 default."""
    chip = tmp_path / "pwmchip7"  # deliberately not pwmchip0
    actuators, _ = build_from_assignments(
        [adopted_pipwm_assignment(channel=0)], sysfs=FakeSysfs(), pwm_chip_root=chip
    )
    assert actuators[0].driver.chip_root == chip


def test_pipwm_skipped_when_no_chip_resolves(monkeypatch):
    monkeypatch.setattr(factory_module, "find_pwm_chip", lambda: None)
    actuators, _ = build_from_assignments([adopted_pipwm_assignment(channel=0)], sysfs=FakeSysfs())
    assert actuators == []  # skipped and logged, not built on pwmchip0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest services/hardware_io/tests/test_factory.py -k identity -v`
Expected: FAIL (no `pwm_chip_root` parameter; chip_root is the pwmchip0 default).

- [ ] **Step 3: Implement**

In factory.py, import `find_pwm_chip` from capabilities; in `build_from_assignments`, add `pwm_chip_root: Path | None = None`; in the pi-pwm branch:

```python
            elif assignment.driver_type == "pi-pwm":
                if pwm_chip_root is None:
                    pwm_chip_root = find_pwm_chip()
                if pwm_chip_root is None:
                    raise TopologyError(
                        "no RP1 PWM0 chip resolved by identity; refusing to build "
                        "on a guessed pwmchip index (spec dd6a68b)"
                    )
                actuators.append(
                    BuiltActuator(
                        PiPwmChannel(
                            int(binding["channel"]),
                            assignment.device_id,
                            sysfs=sysfs,
                            chip_root=pwm_chip_root,
                        ),
                        light_registration(actuator_id=assignment.device_id, driver_id="rp1-pwm"),
                    )
                )
```

(The existing per-assignment `except (KeyError, ValueError, TopologyError)` catch makes the no-chip case a skip-and-log, matching the second test. `find_pwm_chip` reads sysfs once; hoisting the resolution to the top of the function is fine too — either way it must run at most once per build.)

Verify `PiPwmChannel.__init__` accepts `chip_root` as a keyword (pipwm.py ~184: it does, `chip_root: Path = PWM_CHIP_ROOT`).

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/hardware_io/tests -v && uv run mypy services/hardware_io`
Expected: PASS. On the Pi this is behavior-preserving today (pwmchip0 *is* currently the right chip — discovery proves it every boot); the change is which future kernel it survives.

- [ ] **Step 5: Commit**

```bash
git add services/hardware_io
git commit -m "fix(hardware-io): build PiPwm channels on the identity-resolved chip, never the pwmchip0 index (dd6a68b)"
```

---

### Task 6: `bind_device` tells the truth about a device_id conflict

**Files:**
- Modify: `services/api/bellasreef_api/store.py` (`bind_device` create path ~428; new `DeviceIdConflictError`)
- Modify: `services/api/bellasreef_api/app.py` (bind endpoint ~1429: catch → 409)
- Test: `services/api/tests/` (the file that already tests bind — `grep -rl bind_device services/api/tests/`)

**Interfaces:**
- Produces: `class DeviceIdConflictError(RuntimeError)` in store.py (carries `device_id`); `bind_device` raises it when the INSERT's rowcount is 0; the endpoint returns `409` with a message naming both the device_id and the requested channel.

- [ ] **Step 1: Write the failing test**

```python
async def test_bind_existing_device_id_to_new_channel_409s(api_client, seeded_device):
    """Finding 9: 'light-left' bound to channel 2; POSTing it onto free channel
    3 hit ON CONFLICT DO NOTHING, wrote nothing, returned created=True, and
    published an assignment contradicting Postgres."""
    await bind(api_client, device_id="light-left", channel="2")  # setup
    resp = await bind_raw(api_client, device_id="light-left", channel="3")
    assert resp.status_code == 409
    assert "light-left" in resp.json()["detail"]
    # And nothing was published: the registry still says channel 2.
    assert await bound_channel_of(api_client, "light-left") == "2"
```

Follow the existing bind-endpoint test file's fixtures for client, auth, and capability seeding.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest services/api/tests -k "new_channel_409" -v`
Expected: FAIL — today it returns 200 `created: true`.

- [ ] **Step 3: Implement**

store.py:

```python
class DeviceIdConflictError(RuntimeError):
    """The proposed device_id already names a device with a different binding."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"device_id already in use: {device_id!r}")
        self.device_id = device_id
```

In the create path, capture the INSERT result and check it:

```python
result = await conn.execute(
    text("INSERT INTO devices (...) ... ON CONFLICT (device_id) DO NOTHING"), {...}
)
if result.rowcount == 0:
    # The channel-holder check upstream vouched for the CHANNEL;
    # this is the other axis — the proposed id already names a
    # device bound elsewhere. DO NOTHING wrote nothing, and
    # returning created=True here published an assignment that
    # contradicted Postgres (2026-08-23 finding 9).
    raise DeviceIdConflictError(device_id)
```

app.py endpoint, around the `store.bind_device(...)` call:

```python
        try:
            device_id, created = await store.bind_device(...)
        except DeviceIdConflictError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"device_id {exc.device_id!r} already names a device bound to a different "
                "channel. Rename or forget it first.",
            ) from exc
```

The transaction context manager rolls back on the raise, so no partial write.

- [ ] **Step 4: Run suite + types**

Run: `uv run pytest services/api/tests -v && uv run mypy services/api`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api
git commit -m "fix(api): binding an existing device_id to a different channel 409s instead of lying"
```

---

### Task 7: the API gets its metrics endpoint

**Files:**
- Modify: `services/api/bellasreef_api/app.py` (lifespan: start/stop a `MetricsServer`)
- Modify: `deploy/compose.yaml` (api `environment:` gains `BELLASREEF_METRICS_PORT: "9103"` — document-only; the default in code is 9103)
- Test: `services/api/tests/test_background_components.py` (follow its harness)

**Interfaces:**
- Consumes: `bellasreef_service.httpd.MetricsServer(probe=..., registry=..., port=...)` — the exact pattern hardware-io/engine use.
- Produces: a Prometheus endpoint on port 9103 inside the api container, serving a `CollectorRegistry` with at least a `bellasreef_api_requests_total` counter (label: `method`, `status_family`) incremented by one small ASGI middleware, plus the health probe the MetricsServer wraps.

- [ ] **Step 1: Write the failing test**

```python
async def test_metrics_server_starts_with_the_app(api_lifespan_harness):
    """CLAUDE.md day-1 requirement: every service exposes metrics. The API
    shipped without one (2026-08-23 review, cleanup findings)."""
    async with api_lifespan_harness() as app_state:
        body = await http_get("http://127.0.0.1:9103/metrics")
        assert "bellasreef_api_requests_total" in body
```

Pick a free port in the fixture (bind 0 and read it back) rather than hardcoding 9103 in tests.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest services/api/tests -k metrics -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In the lifespan (where AuditWriter etc. are started — `test_background_components.py` shows the wiring), build a `CollectorRegistry`, the counter, and a `MetricsServer(probe=<the existing health callable or a trivial ok-probe>, registry=registry, port=int(os.environ.get("BELLASREEF_METRICS_PORT", "9103")))`; start it on entry, stop on exit. Middleware:

```python
    @app.middleware("http")
    async def _count_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        metrics_requests.labels(request.method, f"{response.status_code // 100}xx").inc()
        return response
```

Match the file's typing conventions (this repo is `mypy --strict`; the middleware may need explicit annotations instead of the ignore — write it however the surrounding decorators are typed).

- [ ] **Step 4: Run suite + types + full check**

Run: `uv run pytest services/api/tests -v && uv run mypy services/api && BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api deploy/compose.yaml
git commit -m "feat(api): metrics endpoint on 9103 — the day-1 requirement the service shipped without"
```

---

### Task 8: PR, merge, deploy, NATS-outage drill

- [ ] **Step 1:** Push branch, PR `fix(resilience): transient faults degrade services instead of killing them`. PR body lists the four resilience findings + three quickies, and the **clock-trust shadow-mode note for David** from Task 4 verbatim. CI green → merge.
- [ ] **Step 2:** `scripts/deploy-pi.sh`; telemetry gate must pass.
- [ ] **Step 3: NATS-outage drill (pre-approved; deferred from batch A).** On the Pi: `docker stop bellasreef-nats-1` (note: manual stop disables its restart policy — the drill MUST end with `docker start bellasreef-nats-1`). Expect within ~31 s: hardware-io trips its actuators (`heartbeat_timeout` — its heartbeat subscription hears nothing); the engine logs suppressed publishes (`no_spine` / `publish_failed`) and KEEPS RUNNING (no restart-count increase — the fix under test). Then `docker start bellasreef-nats-1`; expect: engine reconnects, re-drains assignments, beats resume, schedule converges the light back up under the slew (the batch-A state-forget). Record: `docker inspect -f '{{.RestartCount}}' bellasreef-control-engine-1` before/after (must be equal), log excerpts, timestamps.
- [ ] **Step 4:** Verify fresh telemetry on the wire post-drill (the deploy script's own gate, or `docker logs bellasreef-control-engine-1 --since 5m` showing published commands and the API's stream serving state).

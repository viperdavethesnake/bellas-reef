# Chip State PR 1 (contracts + hardware-io) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put per-chip configuration state on the wire — a retained `ChipState` message per hardware source instance, published by hardware-io after bring-up, so a client can finally see "it took" (frequency, polarity, initialised) instead of trusting a log line.

**Architecture:** New `ChipState` message + `subjects.chip()` in contracts (MINOR); new last-value JetStream stream `BR_CHIP` provisioned by hardware-io beside `BR_CAPABILITY`; publishers hang off the moments the facts become true — `Pca9685Device.initialise()`, the first `PiPwmChannel.open()` per chip, and `discover_w1()` announce time. Best-effort like `_publish_state`: a publish failure logs, never raises into `open()`.

**Tech Stack:** Python 3.13, Pydantic v2, nats-py JetStream, pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-chip-state-on-the-wire-design.md`

## Global Constraints

- Contracts bump is **4.1.0 → 4.2.0** (the spec's "4.0.0 → 4.1.0" is superseded: 4.1.0 shipped with lighting schedules on 2026-08-22). Additive only. `contracts/python/pyproject.toml` AND `deploy/avahi/bellasreef.service`'s `<txt-record>contracts=…</txt-record>` in the same commit (the check.sh `avahi_contracts` gate compares them).
- `mypy --strict` clean (gate scope `contracts/python db services`); ruff lint + format clean. Conventional commits; TDD per task.
- `bench_verified` is NOT a fact on the wire (spec ruling — a bench note in CLAUDE.md must not look like telemetry). `initialise()`'s register writes change in no way.
- Publication is best-effort: failure to publish chip state must never fail `open()` or discovery.
- NATS subject tokens cannot contain `.` — `subjects.chip()` sanitizes the instance token (see Task 1); the MESSAGE field `instance` keeps the raw string.
- Env-gated integration tests: declare skips with `BELLASREEF_ALLOW_ENV_SKIPS=1`; never any non-loopback endpoint.

---

### Task 1: `ChipState` message + `subjects.chip` (contracts 4.2.0)

**Files:**
- Modify: `contracts/python/bellasreef_contracts/messages.py` (new model near `CapabilityAnnouncement`, ~line 216)
- Modify: `contracts/python/bellasreef_contracts/subjects.py` (new helper + `ALL_CHIPS`)
- Modify: `contracts/python/bellasreef_contracts/__init__.py` (exports)
- Modify: `contracts/python/pyproject.toml` (`4.1.0` → `4.2.0`)
- Modify: `deploy/avahi/bellasreef.service` (`contracts=4.1.0` → `contracts=4.2.0`)
- Test: `contracts/python/tests/test_chip_state.py`

**Interfaces:**
- Produces: `ChipState(_Message)` with fields `hardware_source: CapabilitySource`, `instance: str` (min_length 1, max_length 64), `initialised: bool`, `initialised_at: AwareDatetime | None`, `facts: dict[str, str | int | float | bool]`; `subjects.chip(source: str, instance: str) -> str`; `subjects.ALL_CHIPS: Final = f"{ROOT}.chip.>"`.

- [ ] **Step 1: Write the failing tests**

```python
# contracts/python/tests/test_chip_state.py — follow the SPDX header and import
# style of contracts/python/tests/test_schedules.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bellasreef_contracts import ChipState, subjects


def _state(**overrides: object) -> ChipState:
    base: dict[str, object] = {
        "hardware_source": "pca9685",
        "instance": "0x40@1",
        "initialised": True,
        "initialised_at": datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
        "facts": {"pre_scale": 12, "frequency_hz": 502.7, "invrt": False, "address": "0x40"},
    }
    base.update(overrides)
    return ChipState.model_validate(base)


def test_round_trips_through_json() -> None:
    s = _state()
    assert ChipState.model_validate_json(s.model_dump_json()) == s


def test_never_initialised_has_no_timestamp() -> None:
    s = _state(initialised=False, initialised_at=None, facts={})
    assert s.initialised_at is None


def test_unknown_source_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(hardware_source="esp32")


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(bench_verified=True)


def test_chip_subject_sanitizes_dots() -> None:
    # NATS subject tokens cannot contain '.', but real instances do
    # ("1f00098000.pwm"). The subject swaps '.' for '-'; the message field
    # keeps the raw instance.
    assert subjects.chip("pi-pwm", "1f00098000.pwm") == "bellasreef.chip.pi-pwm.1f00098000-pwm"
    assert subjects.chip("pca9685", "0x40@1") == "bellasreef.chip.pca9685.0x40@1"
    assert subjects.ALL_CHIPS == "bellasreef.chip.>"


def test_chip_subject_rejects_empty() -> None:
    with pytest.raises(ValueError):
        subjects.chip("pca9685", "")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest contracts/python/tests/test_chip_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'ChipState'`.

- [ ] **Step 3: Implement**

In `messages.py`, next to `CapabilityAnnouncement`, with the spec's docstring:

```python
class ChipState(_Message):
    """What one hardware source is configured as, right now.

    Published on ``bellasreef.chip.<source>.<instance>`` and retained
    last-value per subject, like a capability announcement — a consumer that
    starts late still learns how the chip is set up. Per hardware source
    instance, not per channel: frequency, polarity, output mode and
    "initialised" are properties of the chip (spec 2026-08-19).
    """

    hardware_source: CapabilitySource
    instance: str = Field(min_length=1, max_length=64)
    initialised: bool
    initialised_at: AwareDatetime | None = None
    #: Facts a client renders as a table. Free-form for the same reason
    #: CapabilityChannel.detail is: they differ per source and no consumer
    #: should switch on them. Keys are stable strings; values scalars.
    facts: dict[str, str | int | float | bool] = Field(default_factory=dict)
```

In `subjects.py`, following the file's helper style (docstring, `validate_token` usage where it fits):

```python
def chip(source: str, instance: str) -> str:
    """Subject for one hardware source instance's retained ChipState.

    Instances carry characters NATS reserves ('.' in "1f00098000.pwm" would
    split the token), so the instance token swaps '.' for '-'. The MESSAGE's
    ``instance`` field keeps the raw string; the subject is an address, not
    the datum.
    """
    if not source or not instance:
        raise ValueError("chip subject needs a source and an instance")
    return f"{ROOT}.chip.{source}.{instance.replace('.', '-')}"
```

and `ALL_CHIPS: Final = f"{ROOT}.chip.>"` beside the other `ALL_*`. Export `ChipState` from `__init__.py` (subjects module is already exported wholesale). Bump `pyproject.toml` to `4.2.0`; bump the avahi TXT record to `4.2.0` in the same commit.

- [ ] **Step 4: Run the contracts suite** — `uv run pytest contracts/python/tests/ -v` — PASS.
- [ ] **Step 5: mypy + ruff, commit**

```bash
uv run mypy --strict contracts/python && uv run ruff check . && uv run ruff format --check .
git add contracts/python deploy/avahi
git commit -m "feat(contracts): ChipState message + chip subject (4.2.0) — per-board facts on the wire"
```

---

### Task 2: `BR_CHIP` stream + `Spine.publish_chip_state`

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/spine.py` (stream config ~line 108 block; publish method beside `publish_capabilities` ~line 302)
- Test: `services/hardware_io/tests/test_spine.py` (append, following how existing publish methods are tested there — read the file first)

**Interfaces:**
- Consumes: Task 1 `ChipState`, `subjects.chip`, `subjects.ALL_CHIPS`.
- Produces: `CHIP_STREAM: Final = "BR_CHIP"`; `async def publish_chip_state(self, state: ChipState) -> None` publishing `state.model_dump_json().encode()` to `subjects.chip(state.hardware_source, state.instance)`.

- [ ] **Step 1: Write the failing tests** — in `test_spine.py`'s existing style (it has fakes/harness for the other publish methods and stream provisioning; mirror exactly). Two tests: (a) `BR_CHIP` appears in the provisioned stream configs with `subjects=[subjects.ALL_CHIPS]`, `retention=LIMITS`, `storage=FILE`, `max_msgs_per_subject=1` — assert against the module's stream-config list the same way existing tests assert `BR_CAPABILITY`'s shape; (b) `publish_chip_state` publishes the JSON-encoded model to the sanitized subject (use instance `"1f00098000.pwm"` and assert the subject is `bellasreef.chip.pi-pwm.1f00098000-pwm`), and raises `RuntimeError("spine not connected")` when unconnected, exactly as `publish_capabilities` does.
- [ ] **Step 2: Run to verify failure.** `uv run pytest services/hardware_io/tests/test_spine.py -v` — FAIL (no CHIP_STREAM / no method).
- [ ] **Step 3: Implement** — add the `StreamConfig` beside `BR_CAPABILITY`'s with a comment in the file's voice (retained last-value so a late consumer still learns how each chip is configured), and:

```python
async def publish_chip_state(self, state: ChipState) -> None:
    """Announce how one hardware source instance is configured.

    Retained last-value per (source, instance): re-initialisation after a
    bus fault republishes, and a consumer that starts late reads the
    current configuration instead of waiting for the next restart.
    """
    if self._nc is None:
        raise RuntimeError("spine not connected")
    await self._nc.publish(
        subjects.chip(state.hardware_source, state.instance),
        state.model_dump_json().encode(),
    )
```

- [ ] **Step 4: Run the hardware-io suite** — `BELLASREEF_ALLOW_ENV_SKIPS=1 uv run pytest services/hardware_io/tests/ -v` — PASS.
- [ ] **Step 5: mypy + ruff, commit** — `feat(hardware-io): BR_CHIP retained stream + Spine.publish_chip_state`

---

### Task 3: Publishers at the three bring-up moments

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/drivers/pca9685.py` (`Pca9685Device` gains a `chip_state()` factory; NO change to what `initialise()` writes)
- Modify: `services/hardware_io/bellasreef_hardware_io/drivers/pipwm.py` (chip-level facts factory)
- Modify: `services/hardware_io/bellasreef_hardware_io/app.py` (the wiring: publish after `open()`, keyed per chip; w1 at announce time — study how `app.py` brings actuators up via `open()` and where `discover_w1`'s announcement is published, and hang the publications there)
- Test: `services/hardware_io/tests/test_chip_state_publish.py` (new)

**Interfaces:**
- Consumes: Task 1 `ChipState`; Task 2 `publish_chip_state`.
- Produces: `Pca9685Device.chip_state() -> ChipState` (instance `f"{hex(address)}@{bus}"`, e.g. `"0x40@1"`); `PiPwmChannel`/its chip owner exposing enough for app.py to build `ChipState` with instance = the chip's device identity (e.g. `"1f00098000.pwm"`); app-level per-chip publish-once keying.

**Facts, verbatim from the spec table (values from what the driver already knows — nothing newly measured):**
- pca9685: `address` (hex string "0x40"), `bus` (int), `pre_scale` (`PCA9685_PRE_SCALE`), `frequency_hz` (`round(PCA9685_OSC_HZ / (4096 * (PCA9685_PRE_SCALE + 1)), 1)` → 502.7), `oscillator_hz` (`PCA9685_OSC_HZ`), `invrt` (`INVRT_ON`), `open_drain` (`OPEN_DRAIN`), `channels` 16, `pre_scale_read_back` (the value `initialise()` read back — capture it on the device when it asserts it).
- pi-pwm: `chip` (pwmchip name), `device` (device identity, "1f00098000.pwm"), `period_ns` (the channel's period, default `DEFAULT_PERIOD_NS` 2_000_000), `frequency_hz` (`1e9 / period_ns` → 500), `polarity` ("normal"), `channels` 4.
- w1-bus: `bus_master` ("w1_bus_master1"), `probes` (count of `28-*` the discovery saw). `initialised=True` means "the bus is present"; `initialised_at` = announce time.

- [ ] **Step 1: Write the failing tests** — fake bus / fake sysfs / fake spine in the style the hardware-io tests already use (read `services/hardware_io/tests/` for the established fakes; `test_pca9685.py` has a fake smbus, `test_app.py`-equivalents show how the spine is faked). Cases: (a) after `Pca9685Device.ensure_initialised()`, `chip_state()` returns `initialised=True`, an aware `initialised_at`, and exactly the fact keys above with the right values (assert `pre_scale_read_back == PCA9685_PRE_SCALE` via the fake bus); (b) the app publishes pca9685 chip state once per chip however many channels open (two channels opened → one publish); (c) pi-pwm chip state published once per chip on first channel open with the fact table above; (d) w1 chip state published at announce with `probes` equal to the fake tree's probe count; (e) a spine whose `publish_chip_state` raises → `open()` still succeeds and a warning is logged (best-effort, mirroring `_publish_state`'s pattern).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** `Pca9685Device`: store `self._pre_scale_read_back` when `initialise()` asserts it; add `initialised_at` stamp in `ensure_initialised`; `chip_state()` assembles the model (no I/O — reads stored values). pipwm: expose the chip identity + period; app.py builds the message. In app.py, key publish-once per chip (a `set[str]` of published instances is enough; a re-initialise path republished naturally because `ensure_initialised` only runs once per process — document that re-publish-on-refault arrives with a future bus-fault story, per spec "published again whenever its state changes" being satisfied by process lifecycle today). Wrap every publish in the same try/except-log shape as `_publish_state`.
- [ ] **Step 4: Run the full hardware-io suite** — PASS; `uv run mypy --strict services/hardware_io` (gate runs `services`); ruff.
- [ ] **Step 5: Commit** — `feat(hardware-io): publish ChipState at bring-up — pca9685 after initialise, pi-pwm first open, w1 at announce`

---

### Task 4: Contract prose + PR

- [ ] **Step 1:** Add the `bellasreef.chip.<source>.<instance>` subject row to `docs/contracts/nats-subjects.md` in its existing table/prose style (retained last-value, MINOR 4.2.0, instance-token sanitization rule), citing the spec.
- [ ] **Step 2:** Controller opens the PR (`feat(chip-state): ChipState on the wire — contracts 4.2.0, BR_CHIP, hardware-io publishers`); CI green; merge; deploy; verify on the hub: `ssh bellasreef.local 'curl -s "http://127.0.0.1:8222/jsz?streams=1"'` shows `BR_CHIP` with one message per brought-up chip, and `docker logs bellasreef-hardware-io-1` shows the publishes.

## Self-review

- Spec coverage: message+subject+stream (T1/T2), three publishers + best-effort + after-open ordering (T3), MINOR + avahi (T1), docs (T4). `GET /api/v1/hardware`, migration, iOS = PR 2/3 by the spec's own Order. `bench_verified` exclusion tested (extra="forbid" test).
- Placeholders: none — every test step names concrete cases and the fact tables carry exact values.
- Type consistency: `ChipState` fields consumed by name in T2 (publish) and T3 (factories); `subjects.chip` sanitization asserted in both T1 and T2.

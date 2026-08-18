# Hardware driver interface — v1

**Status:** locked for v1.0.0 · **Code:** `contracts/python/bellasreef_contracts/driver.py`

This is the contract identified in the PRD (§7.3.3) as the one that cannot be
retrofitted. Once a dozen drivers exist against a loose interface, tightening it
means rewriting all of them — so it is fixed now, with one implementation
planned and none written.

Everything below applies to `hardware-io` only. No other service imports this
module, and no driver knows what a reef is.

---

## 1. The two rules that come from measured hardware

Both are drawn from "Verified host facts" in CLAUDE.md, and both are encoded in
types rather than left in prose, because both are easy to violate by accident.

### 1.1 GPIO lines are addressed by chip label, never by index

`/dev/gpiochipN` numbering has moved between kernel releases on this board. On
the target, `gpiochip0` is `pinctrl-rp1` with 54 lines — the 40-pin header. The
four `gpio-brcmstb` chips are internal SoC lines and are **not** header pins.
Hardcoding an index is a bug that survives testing and breaks on a kernel
upgrade, silently driving the wrong pin.

`GpioLine` therefore has no index field at all:

```python
GpioLine(chip_label="pinctrl-rp1", offset=4)  # valid
GpioLine(chip="0", offset=4)  # ValidationError — no such field
```

Resolution is by label at open time, via libgpiod v2. `sysfs` GPIO
(`/sys/class/gpio`) is forbidden and is absent on this board regardless.

### 1.2 A slow bus must never dictate control-loop timing

A DS18B20 blocks about **750 ms per conversion** at 12-bit resolution, and the
1-Wire bus is serialized: N probes read naively cost N × 750 ms. That is an
eternity for a scheduler deciding whether a heater stays on.

The cost belongs inside the driver. Concretely, an implementation must:

- offload the blocking read (`asyncio.to_thread`) so the event loop keeps running;
- arbitrate its own shared bus internally, so two probes on one bus queue against
  each other and not against anything else;
- declare an honest `poll_interval_s` — a DS18B20 cannot truthfully produce
  fresh values faster than its conversion time, and claiming otherwise just
  republishes stale numbers with new timestamps;
- return `quality="fault"` on timeout rather than the last good value.

**An implementation whose `read()` can block the event loop violates this
contract even if it returns correct values.**

The caller's obligation is the mirror image: poll each driver as an independent
task on that driver's declared cadence. Never iterate drivers sequentially in one
loop — that reintroduces exactly the coupling this rule removes.

> Note on sysfs: the 1-Wire read path *is* sysfs
> (`/sys/bus/w1/devices/28-*/w1_slave`), because that is the only interface the
> `w1-therm` kernel driver exposes. This is not the forbidden sysfs GPIO. The
> prohibition is on `/sys/class/gpio`, which is deprecated; `w1-therm` has no
> character-device equivalent and using it is correct.

## 2. Addressing types

| Type | Purpose | Notes |
|---|---|---|
| `GpioLine` | one GPIO line | label + offset, no index |
| `I2CAddress` | bus + 7-bit address | address bounded `0x03`–`0x77` |
| `OneWireDevice` | 1-Wire device id | `^[0-9a-f]{2}-[0-9a-f]{12}$` |

On the target, `/dev/i2c-1` is the usable bus; `i2c-13` and `i2c-14` are HDMI
DDC and must be ignored. Expected day-1 addresses: PCA9685 at `0x40`, ADS1115 at
`0x48`, MCP23017 at `0x20`.

## 3. `SensorDriver`

```python
@runtime_checkable
class SensorDriver(Protocol):
    @property
    def driver_id(self) -> DeviceId: ...
    @property
    def sensor_type(self) -> DeviceId: ...
    @property
    def poll_interval_s(self) -> float: ...
    @property
    def read_timeout_s(self) -> float: ...

    async def read(self) -> SensorSample: ...
    async def calibrate(self, points: Sequence[CalibrationPoint]) -> CalibrationResult: ...
```

`read()` **must not raise on hardware failure.** Losing a probe is an expected
operating condition on a wet, vibrating, corrosive installation — not an
exception. Return `SensorSample(quality="fault", value=None, ...)` and let the
control engine decide, which for temperature means safe state plus an alert,
never last-known-value control (PRD R5).

`SensorSample` carries `raw` alongside the calibrated `value` so a bad
calibration is diagnosable after the fact instead of silently baked into
history.

## 4. `ActuatorDriver`

```python
@runtime_checkable
class ActuatorDriver(Protocol):
    @property
    def driver_id(self) -> DeviceId: ...
    @property
    def actuator_id(self) -> DeviceId: ...
    @property
    def safe_state(self) -> ActuatorLevel: ...

    async def open(self) -> None: ...
    async def apply(self, level: ActuatorLevel) -> None: ...
    async def drive_safe(self) -> None: ...
    async def read_back(self) -> ActuatorLevel | None: ...
```

`open()` brings the output up — export the channel, wake the chip, write the
prescaler — and `hardware-io` calls it on every actuator it builds, before the
supervisor asserts safe state, unconditionally. It is a **required** member
(since 2026-08-18), not an optional hook: an optional hook is one the type
checker cannot see missing, and on 2026-08-17 a driver whose chip setup was
written and tested shipped without it — `hardware-io` duck-typed past the gap
and the bench measured the chip running on whatever the previous process had
left in its registers. A driver with nothing to bring up returns; a driver
that raises is skipped, alone, and logged. Must be idempotent for the
hardware it touches, because sixteen channels may share one chip.

`drive_safe()` is the last line of defence and has the hardest requirement in
this document: **it must work when the spine is down, the control engine is
gone, and the database is unreachable** — because that is precisely when it gets
called. An implementation that publishes to NATS, writes to Postgres, or awaits
anything network-bound inside `drive_safe()` is wrong.

`read_back()` is what makes a stuck relay detectable. Returning `None` is
honest for hardware that cannot report actual state; returning the last
commanded value is not, and would turn "confirmed" into a lie.

## 5. Fake implementations

Every driver ships with a fake satisfying the same Protocol. Engine and
control-loop tests run against fakes exclusively; CI never touches hardware.

Fakes must be able to misbehave on demand — return faults, stall past
`read_timeout_s`, refuse to reach the commanded level — because the failure paths
are the ones worth testing and the only ones a real tank will exercise for you
unprompted.

Tests that require real hardware are marked `@pytest.mark.hardware` and are
excluded from the default run.

## 6. Registration

A driver does not register itself. `hardware-io` publishes an
`ActuatorRegistration` per actuator, and the model rejects any registration
missing `safe_state`, `max_runtime_s`, or `heartbeat_timeout_s`. The same rule is
enforced at rest by `ck_devices_actuator_declares_failure_behaviour` in Postgres.

Two layers, deliberately. The safety framework should not have a single point of
bypass.

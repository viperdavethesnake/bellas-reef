# NATS subject schema — v1

**Status:** locked for v1.0.0 · **Contract version:** 1.0.0 ·
**Code:** `contracts/python/bellasreef_contracts/subjects.py`, `.../messages.py`

This is the integration surface. A phase-2 ESP32 spoke joins the running system
by speaking these subjects and nothing else changes — no edits to the control
engine, the API, or any client. That property is the reason this document exists
before any service does.

Build subjects only through `bellasreef_contracts.subjects`. Never format one
with an f-string at a call site; a stray `.` in an id silently re-shapes the
subject tree.

---

## 1. Token rules

Every id token matches `^[a-z0-9][a-z0-9_-]{0,63}$`.

Lowercase, alphanumeric, `_` and `-`, 1–64 characters, not starting with a
separator. This is not cosmetic: NATS treats `.`, `*` and `>` as structural, so
an unvalidated id could turn one device's command subject into a wildcard
subscription over every device. `subjects.validate_token()` enforces it and is
called by every builder.

## 2. Subject taxonomy

| Subject | Payload | Transport | Publisher |
|---|---|---|---|
| `bellasreef.sensor.<type>.<id>` | `SensorReading` | core pub/sub | hardware-io |
| `bellasreef.cmd.<class>.<id>` | `ActuatorCommand` | **JetStream** `BR_CMD` | control-engine |
| `bellasreef.state.<id>` | `ActuatorState` | **JetStream** `BR_STATE` | hardware-io |
| `bellasreef.heartbeat.<component>` | `Heartbeat` | core pub/sub **only** | every service |
| `bellasreef.audit.<category>` | audit envelope | **JetStream** `BR_AUDIT` | any |
| `bellasreef.registry.<id>` | `ActuatorRegistration` | core pub/sub | hardware-io |
| `bellasreef.chip.<source>.<instance>` | `ChipState` | **JetStream** `BR_CHIP` | hardware-io |
| `bellasreef.host.status` | `HostStatus` | **JetStream** `BR_HOST` | hardware-io |

`<class>` is the actuator class (`binary`, `pwm`). `<type>` is the sensor type
token (`temp`, `ph`, …). `<source>` is the hardware source (`pi-pwm`,
`pca9685`, `w1-bus`) and `<instance>` identifies one instance of that
source — a chip's I²C address, a PWM block's device path, a bus master name.

`<instance>` is the one token in this table that does not pass through
`validate_token()` (§1): it can legitimately carry characters NATS reserves,
such as the `.` in `1f00098000.pwm` (the RP1 PWM0 block's device path), which
would otherwise split the subject into an extra level. `subjects.chip()`
swaps `.` for `-` in the subject token only — the `ChipState.instance` field
on the payload keeps the original string. The subject is an address, not the
datum.

### Why some subjects are deliberately not durable

**Heartbeats are never persisted.** This is a safety property, not an
optimisation. A heartbeat replayed from a stream would make a dead
control-engine look alive to hardware-io — defeating the exact mechanism that is
supposed to drive actuators to their safe state. Heartbeats are core pub/sub,
fire-and-forget, and a missed one must be indistinguishable from a dead sender.

**Sensor telemetry is not persisted on the spine.** History belongs in
VictoriaMetrics. Buffering readings in JetStream would mean a consumer coming
back online gets a burst of stale measurements, and a control loop acting on a
five-minute-old temperature is worse than one acting on none.

**Registrations are announcements.** Postgres `devices` is the system of record;
the subject exists so a running control-engine learns about a new device without
polling.

## 3. JetStream layout

**API correction (2026-08-09).** An earlier draft of this document showed a
`nats.jetstream` API with `timedelta` durations and string-literal policies.
That API belongs to a separate, future package — it does **not** exist in
`nats-py` 2.15.0, which is what we install. Verified by introspecting the
installed package, not by reading docs.

The real API is `nats.js`: durations are **floats in seconds** and policies are
**enums** (`RetentionPolicy`, `AckPolicy`, …). Everything below reflects that.

### `BR_CMD` — durable actuator commands

```python
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

StreamConfig(
    name="BR_CMD",
    subjects=["bellasreef.cmd.>"],
    retention=RetentionPolicy.WORK_QUEUE,  # consumed once by hardware-io, then gone
    storage=StorageType.FILE,
    discard=DiscardPolicy.OLD,
    max_age=3600.0,  # seconds. Backstop only; see §4
    duplicate_window=300.0,  # seconds
)
```

Consumer:

```python
from nats.js.api import AckPolicy, ConsumerConfig

ConsumerConfig(
    durable_name="hardware-io",
    ack_policy=AckPolicy.EXPLICIT,
    ack_wait=5.0,  # seconds
    max_deliver=3,
)
```

**Workqueue retention permits exactly one consumer per filter subject.**
Attempting a second returns `filtered consumer not unique on workqueue
stream`. That is fine for v1 — `hardware-io` is the only subscriber — but it
is a real constraint on anything later that wants to *observe* commands
(a shadow-mode recorder, a second spoke, an external integration). Such a
consumer cannot simply attach to `BR_CMD`; it needs either a non-overlapping
filter or a separate mirrored stream. Discovered against a live broker, not
inferred.

`max_deliver=3` is deliberate. Infinite redelivery of a command that keeps
failing is how a poison message becomes an outage, and by the third attempt the
command has usually expired anyway — at which point delivering it again is not
just useless but dangerous.

### `BR_STATE` — last-known actuator state

```python
StreamConfig(
    name="BR_STATE",
    subjects=["bellasreef.state.>"],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.FILE,
    max_msgs_per_subject=1,  # exactly the current state, nothing older
)
```

One message per subject means a restarting service can fetch the current state
of every actuator without asking hardware-io and without replaying history.

### `BR_CHIP` — retained per-instance chip state

```python
StreamConfig(
    name="BR_CHIP",
    subjects=["bellasreef.chip.>"],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.FILE,
    max_msgs_per_subject=1,  # exactly the current configuration, nothing older
)
```

Same shape as `BR_STATE`: one retained message per subject, so a client that
starts late learns how each chip is configured without waiting for
hardware-io to restart. `ChipState` is a fact about the *chip* — frequency,
polarity, output mode, whether `initialise()` has run — not about any one
channel, which is why the key is `(hardware_source, instance)` rather than
the device id every other stream in this document keys on.

hardware-io publishes at three bring-up moments, one per hardware source
type: for a PCA9685, after `Pca9685Device.initialise()` runs on the first
channel that opens it; for the RP1 PWM block, on its own first channel's
`open()`; for a 1-Wire bus master, at capability-announce time, since a probe
has no `open()` step of its own and "initialised" there means "the bus is
present". Each moment is keyed on `(hardware_source, instance)`, not on the
source alone, so a second chip of the same type — a second PCA9685 on the bus,
say — publishes its own message rather than being skipped as a duplicate.
Publication is best-effort, like `_publish_state`: a failure is logged and
never raised into `open()` or capability discovery, and only a successful
publish is remembered, so a later channel on the same chip gets a retry rather
than permanent silence. Each `(hardware_source, instance)` is published once
per process — the stream and message both support a later republish (e.g.
after re-initialising past a bus fault), but hardware-io does not yet trigger
one; that is a follow-up, not a gap in this contract.

`ChipState` and `BR_CHIP` are the wire half of the design in
`docs/superpowers/specs/2026-08-19-chip-state-on-the-wire-design.md`
(ruled 2026-08-18: option A, a per-chip Hardware surface, not a key in a
capability's `detail` and not a field on the adopted device row). The API
consumer, its migration, and the iOS Hardware leaf are later PRs per that
spec's Order — this stream and its publishers are the whole of what ships
here.

### `BR_AUDIT` — audit transport

```python
StreamConfig(
    name="BR_AUDIT",
    subjects=["bellasreef.audit.>"],
    retention=RetentionPolicy.WORK_QUEUE,
    storage=StorageType.FILE,
    max_age=604800.0,  # 7 days, in seconds
)
```

This is a delivery buffer, not the archive. Postgres `audit_log` is the system of
record and is append-only by trigger. Seven days is how long the writer may be
down before audit events are lost, which is the number to revisit if that ever
feels tight.

## 4. Command lifecycle

Every `ActuatorCommand` carries `idempotency_key` and `expires_at`. Both are
required by the model — a command without them cannot be constructed.

**Idempotency.** The publisher sets the `Nats-Msg-Id` header to
`str(idempotency_key)`. JetStream drops duplicates within the stream's
`duplicate_window`. For dosing, `dosing_journal.idempotency_key` is `UNIQUE`, so
a duplicate that somehow survives the broker still cannot dose twice. Two layers,
because dosing twice is not recoverable.

```python
await js.publish(
    subjects.cmd("binary", "ato-pump"),
    command.model_dump_json().encode(),
    headers={"Nats-Msg-Id": str(command.idempotency_key)},
)
```

**Expiry.** `expires_at` in the payload is **authoritative**. The consumer
re-checks it against its own clock immediately before actuating:

```python
if command.is_expired(datetime.now(UTC)):
    await audit(...)  # dropped, with reason
    await msg.ack()  # ack so it is not redelivered
    return
```

Broker-side TTL (`max_age`, `Nats-TTL`) is defence in depth. It is not relied
upon, for two reasons: it is a broker policy that can be reconfigured out from
under the safety property, and the consumer's clock is the one that matters. On
this hardware that clock is not free — there is no RTC battery, so any service
consuming commands must be ordered `After=time-sync.target` and must treat an
unsynchronised clock as a fault that holds actuators at safe state.

An expired command is **dropped and audited, never executed late.**

## 5. Versioning

The contract version is the version of the `bellasreef-contracts` package, and
every message carries `schema_version`.

| Change | Bump | Migration |
|---|---|---|
| New subject; new optional field on a **new** message type | MINOR | none |
| Clarified docs, tightened validation that rejects nothing previously valid | PATCH | none |
| Subject shape change; field added, removed, renamed, or re-typed on an existing message; semantic change | **MAJOR** | required |

**Adding a field to an existing message is a MAJOR change.** That is stricter
than most schemas and it follows from `extra="forbid"` on every model: an older
consumer receiving an unknown field rejects the message rather than ignoring it.

That strictness is chosen, not accidental. In a system where a firmware typo on a
spoke could silently mean a dose is misread, loud rejection beats quiet
tolerance. The cost is that field additions need a migration.

**contracts 3.0.0 adds the required control-authority axis** — `control_authority`,
`failsafe_capable`, `transport` — to `ActuatorRegistration`, and makes the R1
safety triple conditional on it (docs/device-classes.md §2). Same pre-release
exception as below, and for a stronger reason: the alternative is that every
registration written before the change silently changed meaning, with no way to
tell from the data which guarantee a historical record was asserting.

**contracts 4.0.0 makes `open()` a required member of `ActuatorDriver`**
(docs/contracts/driver-interface.md §4). No subject, payload or OpenAPI path
changes — the wire is byte-for-byte 3.8.0 — but the driver interface is the
third versioned contract and a required member added to a Protocol breaks
every implementer that lacks it, so under our own table this is MAJOR, and
calling it 3.9.0 would tell someone building a driver against 3.x that
nothing had changed for them. Honest version. No dual-publish migration
because the change touches no subject root — the migration path below
protects consumers of the wire, and the wire did not move; the only
implementers of the Protocol are `hardware-io`'s two drivers and its fakes,
updated in the same commit. Clients see only the number: the API's `/info`
reports it, and iOS re-pins its vendored spec to match. Ruled 2026-08-18.

**contracts 4.2.0 adds `ChipState`, the `bellasreef.chip.<source>.<instance>`
subject, and `BR_CHIP`** (§2, §3), published by hardware-io at three bring-up
moments (pca9685, pi-pwm, w1-bus — see §3). A new message type and a new
subject; nothing existing changes, so this is MINOR under the table above —
same class of change as 3.x's additive subjects, even though `<instance>` is
the first token to legitimately bypass `validate_token()` (§1, §2). The API
consumer (`GET /api/v1/hardware`) and the iOS Hardware leaf are separate PRs,
per `docs/superpowers/specs/2026-08-19-chip-state-on-the-wire-design.md`'s
Order; this bump covers only what ships here — the message, the subject, the
stream, and the three publishers.

**contracts 4.3.0 adds `HostStatus`, the `bellasreef.host.status` subject,
and `BR_HOST`** — the hub machine's own vitals (load, memory, SoC
temperature, uptime), published by hardware-io every 30 s and retained
last-value, consumed by the API for `GET /api/v1/hub-status` and the iOS
System tab's Hub status leaf. A new message type and a new subject; nothing
existing changes, so MINOR under the table above. The subject is a fixed
singleton — phase 1 is one hub, and a phase-2 spoke identifies itself in the
envelope's `source`, the subject being an address rather than the datum
(same reasoning as `<instance>` on `bellasreef.chip.*`). Design:
`docs/superpowers/specs/2026-08-31-hub-status-design.md`.

**contracts 4.4.0 adds `GET /api/v1/history/export`** (UX review D7): raw
samples for one device over one window, as CSV or JSON, capped at 31 days.
No subject, no payload and no driver-interface change; the wire is
byte-for-byte 4.3.0. It is an additive OpenAPI path, so MINOR under the table
above, and the number is what the iOS client re-pins its vendored spec to.
The share-sheet half ships in the iOS repo.

### Pre-release exception (expires at the first tagged release)

**contracts 2.0.0 added the required `role` on `ActuatorRegistration` without
the dual-publish migration.** That is a deliberate, bounded exception, taken
because the migration path protects *consumers* and there were none: nothing
subscribed to `bellasreef.registry.>`, there were zero git tags, the package
had never been published, and the only two users of the model are in this repo
and were updated in the same commit.

The version number still says 2.0.0, because under our own rule this *is*
breaking and calling it 1.1.0 would mislead anyone later building against 1.x.
Honest version, skipped ceremony.

**This exception expires at the first tagged release and does not renew.**
Once anything outside this repository can depend on the contract, the
dual-publish path below is mandatory. An exception without an expiry becomes a
habit.

**Migration path for a MAJOR bump:** publish both versions concurrently under a
new subject root (`bellasreef2.…`), move consumers over, then retire the old
root. The `bellasreef.` root is fixed for v1 — version lives in the payload and,
when that is not enough, in a new root.

## 6. Worked example

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from bellasreef_contracts import ActuatorCommand, subjects

now = datetime.now(UTC)
command = ActuatorCommand(
    message_id=uuid4(),
    emitted_at=now,
    source="control-engine",
    actuator_id="ato-pump",
    actuator_class="binary",
    level={"kind": "binary", "on": True},
    idempotency_key=uuid4(),
    expires_at=now + timedelta(seconds=30),
    reason="ato: level below setpoint",
)

subject = subjects.cmd(command.actuator_class, command.actuator_id)
# -> "bellasreef.cmd.binary.ato-pump"
```

A 30-second expiry on an ATO command is a design statement: if the spine is
backed up by more than half a minute, the reason that command was issued is no
longer known to be true, and the safe response is to not top off.

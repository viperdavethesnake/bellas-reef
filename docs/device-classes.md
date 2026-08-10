# Device classes and control authority — v1

**Status:** direction settled, pre-implementation · **Owner:** David / Bella's Reef LLC
**Blocks:** OpenAPI freeze; telemetry writer (VictoriaMetrics label set); any
third-party fixture support.
**Traces to:** PRD R1–R4 (safety framework), R7 (lighting), R10a/R10b (drivers),
R11 (telemetry), R12 (alerting), §7.3.3 (driver interface contract).

---

## 1. The problem

The safety framework assumes something that is not true of every device we will
eventually want in the system.

R1 requires every actuator registration to declare a safe state, a maximum
continuous runtime, and a heartbeat timeout — and it requires that hardware-io
can *drive the device to that safe state* when the heartbeat lapses. That
guarantee holds for a PCA9685 channel, a GPIO relay, or a 0–10V analog output.
It does not hold for a network-attached fixture running its own firmware
schedule.

Concretely, take a Kessil X-series light reached through its WiFi dongle:

- It runs its internal program whether the hub is alive or not. We have no
  authority over it, only influence.
- Writes are best-effort over 2.4GHz WiFi to a device with a well-documented
  history of dropping its controller connection.
- There is no safe state we can command. Cutting the outlet is not "safe" for a
  light — it is a coral stressor, and it strands the fixture's own knob state.
- Round-trip latency is 10²–10³ ms. The ramp engine cannot drive control points
  at PCA9685 cadence.

Registering that fixture as `actuator_class: pwm, role: light` would pass
validation while the guarantee it asserts is fiction. That is the failure mode
this document exists to prevent: **a component that looks present while the wire
to it is dead.** We have now hit that pattern three times in one session — the
sensor publish path, the staleness indicator computed against `Date()`, and the
unverified VictoriaMetrics writer. Encoding it into the safety contract would
make it permanent.

## 2. The authority axis

`actuator_class` describes *what the device is electrically* (binary, pwm).
`role` describes *what it does in the tank* (light, heater, pump, doser,
outlet). Neither says anything about **whether we can actually make it obey**.
That is a third, orthogonal axis, and it is the one the safety framework
actually depends on.

Add to actuator registration:

```
control_authority: authoritative | advisory | observe_only
failsafe_capable:  bool
transport:         local | network
```

### 2.1 `authoritative`

We own the device. Commands are synchronous, deterministic, and verifiable at
the electrical layer. Requires `failsafe_capable: true` and `transport: local`.

- Full R1–R4 apply. Registration is rejected without the complete safety triple.
- Participates in fail-safe drills (G2). A drill failure is a release blocker.
- Eligible for closed-loop control (R5 temperature, R6 ATO, R9 dosing).
- Day-1 members: PCA9685 PWM channels, GPIO relay outputs. Later: 0–10V analog
  out via DAC, and anything else we drive directly off the Pi.

### 2.2 `advisory`

We send intent. The device may or may not comply, and we may or may not learn
which. Typically `transport: network`, `failsafe_capable: false`.

- R4 still applies in modified form: commands are durable, idempotent, and carry
  an expiry, but a dropped command is an expected outcome, not an incident.
- R1's safety triple is **not** required, and must not be faked. Registration
  rejects a declared `safe_state` on an advisory device rather than accepting a
  value it cannot enforce.
- Excluded from fail-safe drills. Including them would produce a green drill
  result that means nothing.
- Never permitted in a closed control loop. An advisory device may be scheduled
  and may be commanded manually; it may not be the actuator in a feedback loop
  whose sensor governs livestock safety.
- Must render visibly distinct in every client surface. See §5.

### 2.3 `observe_only`

Registered for coordination, never written to. The fixture runs its own program;
the hub models its photoperiod so that dosing windows, feed mode, maintenance
mode, and PAR bookkeeping can reason about it.

- No commands are ever emitted. The command path is closed at registration, not
  by convention.
- Zero safety surface, zero protocol risk, no bench dependency.
- This is the correct default for any third-party fixture we have not yet
  established a control path to — which today is every Kessil X-series light.

## 3. Service placement

hardware-io stays what it is: Postgres-free, libgpiod-bound, safety-critical,
and small. It handles `authoritative` devices only.

`advisory` and `observe_only` devices get a separate service on the spine —
working name `vendor-bridge`. It publishes to the same NATS subjects and
satisfies the same driver interface contract, but it is architecturally
permitted to fail without touching the tank.

Rationale: a vendor bridge accumulates mDNS discovery, retry and backoff state,
per-vendor auth material, HTTP or BLE client stacks, and vendor firmware
quirks. None of that belongs in the process that is allowed to drive a heater.
The blast radius has to be separated at the process boundary, not by discipline.

| | hardware-io | vendor-bridge |
|---|---|---|
| Authority | `authoritative` only | `advisory`, `observe_only` |
| Transport | local (I2C, GPIO, 1-wire) | network (IP, BLE) |
| Postgres | none | none — config via API, state via NATS |
| Fail-safe drills | required, gating | not applicable |
| Crash impact | tank safety event | degraded feature, alert only |
| Command semantics | synchronous, verified | best-effort, journaled |

## 4. Telemetry: why this blocks the VictoriaMetrics writer

VictoriaMetrics series identity is the label set. Once the spine starts writing
actuator telemetry, every series is stamped with whatever labels exist at that
moment. Adding a label later forks the series: history under one identity, new
data under another, and every range query that straddles the change is either
wrong or needs a rewrite rule for the life of the install.

The failure this prevents is specific. A PCA9685 duty of 0.6 means the pin is at
0.6 — measured, local, verifiable. An advisory duty of 0.6 means *we asked for
0.6 and the network did not return an error*, possibly twenty seconds ago.
Charted without a distinguishing label, those two lines are visually identical,
and one of them is a claim wearing the costume of a measurement.

Therefore:

- `control_authority` is a required label on all actuator state series.
- Advisory series additionally carry `command_acked` and the age of the last
  successful exchange, so a chart can show *when we stopped knowing*.
- Alert episodes (R12) carry the authority of the device that produced them. An
  episode on an advisory device did not trigger a fail-safe, because there is no
  fail-safe to trigger; recording it identically to an authoritative episode
  makes the entire alert history untrustworthy the moment the first vendor
  bridge ships.

**Sequencing consequence:** the schema change lands in the same Alembic
migration window as the telemetry writer, ahead of the first write. Not after.

## 5. Client surfaces

The app already has a rule that red appears nowhere except safety. This is the
adjacent rule: **the client never renders an advisory reading with the same
visual weight as a measured one.**

- Authoritative actuators render as they do now — solid value, live state.
- Advisory actuators render the commanded value with an explicit "commanded"
  treatment and the age of the last successful exchange. When that age exceeds
  the configured staleness window, the control degrades to amber, the same way
  an unreporting probe does.
- Observe-only fixtures render as context, not control. Photoperiod band on the
  Lighting timeline, no sliders, no override affordance.
- The Tank tab's safety status line derives from authoritative devices only. An
  advisory device that has gone silent is amber (attention), never teal (all
  clear) and never red (safety) — it is not a safety signal, because it never
  offered a safety guarantee.

## 6. Migration note

This is additive to the schema but semantically breaking for anything already
registered: every existing actuator becomes `control_authority: authoritative`,
`failsafe_capable: true`, `transport: local` — which is accurate today, since
every device we drive is a PCA9685 channel or a GPIO pin.

The reason to do it before the OpenAPI freeze is the same argument that landed
`role`, only stronger. `role` was a presentation concern. This is a P0 safety
field: if it is added later, every registration written before the change
silently changed meaning, and there is no way to tell from the data which
guarantee a historical record was actually asserting.

## 7. Worked example — Kessil

Verified as of 2026-08-10. Kessil publishes no protocol documentation; the
K-Link wire format is undocumented and I found no public reverse-engineering
work on it. Treat it as unknown, not as "probably RS-485."

| Generation | Control surface | Classification |
|---|---|---|
| A360WE/NE, A80, A160, AP700 | 0–10V analog, 3.5mm TRS — tip = intensity, ring = color, sleeve = ground | `authoritative` once the analog output stage exists |
| A360X, A360XE, A500X, AP9X | K-Link only. Confirmed by Kessil as **not** 0–10V compatible; no analog input exists on the fixture | `observe_only` today |
| X-series + WiFi Dongle | Dongle joins the 2.4GHz LAN, takes a DHCP lease, speaks to the Kessil app and to Neptune Apex via IoTa | `advisory` if and when a bridge exists |

What is known about K-Link: USB-C connector, two-way, the master detects fixture
count and status, up to 32 fixtures per chain, and the fixture can power the
controller. Framing, addressing, and electrical layer are not published.

The dongle is the tractable target. Neptune's IoTa integration attaches over the
LAN and the Apex module view displays the dongle's IP address, which establishes
that a local IP control surface exists on our own network. Capturing it requires
no circumvention of any protection measure. Given the LLC and the dual-licensing
structure, protocol work goes past IP counsel before it ships in a commercial
build — DMCA §1201(f) interoperability is the relevant doorway, and that is
counsel's call, not engineering's.

### 7.1 Legacy 0–10V path

The PCA9685 open-drain path drives the Mean Well XLG-AB dimming input, but
Kessil's 0–10V input wants an actual analog voltage. An MCP4728 DAC into a
rail-to-rail op-amp gain stage is cleaner than RC-filtering PWM and yields a
genuinely deterministic, fail-safe-capable second-vendor actuator.

One UI consequence: a legacy Kessil exposes two axes (intensity, color), not
independent per-wavelength channels. The Lighting tab's per-channel spectrum bar
needs a two-axis variant. This is a fixture *capability* question, not a
per-vendor special case — worth generalizing when the render path is built.

### 7.2 Priority

1. `observe_only` registration. Ships now, no protocol work, covers both
   generations, and proves the authority axis end to end before anything
   depends on it. Also unblocks Lighting UI work that is otherwise gated behind
   the PCA9685 bench session, since it actuates nothing.
2. Legacy 0–10V as `authoritative`, behind the analog output stage.
3. Dongle LAN protocol as `advisory`. Real work, unknown payoff until the
   capture is done — but it is the piece that generalizes. The same bridge
   pattern covers Red Sea (`ha-reefbeat-component` already does this against
   ReefBeat's local API) and later Radion/Mobius over BLE.
4. K-Link wire level. Not until 1–3 are done, and not without a Spectral
   Controller X to sniff a known-good master against a sacrificial fixture.

## 8. Open questions

| # | Question | Blocks |
|---|---|---|
| D1 | Does `advisory` need a per-device staleness window, or does one global value suffice? Leaning per-device — a WiFi light and a BLE pump have different plausible silences. | vendor-bridge config schema |
| D2 | Can an `observe_only` device be promoted to `advisory` in place, or is it a re-registration? Promotion changes the telemetry label set, which forks the series — argues for re-registration with a documented lineage field. | schema, migration policy |
| D3 | Does `vendor-bridge` ship in v1 at all, or does v1 ship the classification with only `authoritative` and `observe_only` populated? Leaning the latter: the contract is the thing that cannot be retrofitted, the bridge is not. | v1 scope |

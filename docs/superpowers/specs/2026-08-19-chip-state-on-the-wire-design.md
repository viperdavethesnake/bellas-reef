# Chip state on the wire — the Hardware leaf's per-board facts

Spec only, for David's review. Ruled 2026-08-18: **option A** — per-chip state
lives on its own surface (the System → Hardware leaf), not as a key in the
capability `detail` (identity only, per #38) and not as a field on the adopted
device row. Recorded in CLAUDE.md ("Chip state on the wire") and in the UX
review's Tier C answer (C1/C2). Not built unattended: it is a contract change.

## Motivation

Stage 2 (2026-08-17) found the PCA9685 running on Stage 1's leftover prescaler
because `initialise()` had never been called — the chip was configured by
whatever the previous process left in it, and nothing above hardware-io could
tell. #40 fixed the call and #44 made `open()` a required Protocol member, so
the gap is closed in code. What is still missing is *visibility*: the operator
cannot see, from the app, whether the chip was initialised, at what frequency,
with which polarity, or how many channels it drives. hardware-io logs it
(`pca9685 initialised address=0x40 pre_scale=12 invrt=false`); the log is not a
client surface. David's question at the time was "how do I know it took?" —
this is the answer.

## What is announced

One message per **hardware source instance** (a chip, a PWM block, a bus
master), published by hardware-io **after** the source is brought up — i.e.
after `open()` on its first channel — and again whenever its state changes
(re-initialise after a bus fault, say). Not per channel: frequency, polarity,
output mode and "initialised" are properties of the chip, which is exactly why
`Pca9685Device` is split from `Pca9685Channel`.

```python
class ChipState(_Message):
    """What one hardware source is configured as, right now.

    Published on ``bellasreef.chip.<source>.<instance>`` and retained
    last-value per subject, like a capability announcement — a consumer that
    starts late still learns how the chip is set up.
    """
    hardware_source: CapabilitySource        # "pi-pwm" | "pca9685" | "w1-bus"
    instance: str                             # "0x40@1", "1f00098000.pwm", "w1_bus_master1"
    initialised: bool                         # this process configured it
    initialised_at: AwareDatetime | None      # when; None if it never was
    #: Facts a client renders as a table. Free-form for the same reason
    #: CapabilityChannel.detail is: they differ per source and no consumer
    #: should switch on them. Keys are stable strings; values scalars.
    facts: dict[str, str | int | float | bool]
```

Facts, per source, from what the driver already knows (nothing new is measured):

| Source | facts |
|---|---|
| `pca9685` | `address` "0x40", `bus` 1, `pre_scale` 12, `frequency_hz` 502.7 (from `PCA9685_OSC_HZ`), `oscillator_hz` 26770000, `invrt` false, `open_drain` false, `channels` 16, `pre_scale_read_back` 12 (the assert in `initialise()` already reads it) |
| `pi-pwm` | `chip` "pwmchip0", `device` "1f00098000.pwm", `period_ns` 2000000, `frequency_hz` 500, `polarity` "normal", `channels` 4 |
| `w1-bus` | `bus_master` "w1_bus_master1", `probes` 1 |

`bench_verified` is **not** a fact on the wire. It is a note in CLAUDE.md about a
measurement; putting it in a message would make a bench ruling look like
telemetry.

## Contract

- New message type `ChipState` in `bellasreef_contracts.messages`; new subject
  helper `subjects.chip(source, instance)`; `ALL_CHIPS = "bellasreef.chip.>"`.
- New JetStream stream `BR_CHIP`, `max_msgs_per_subject=1`, LIMITS retention —
  provisioned by hardware-io next to `BR_CAPABILITY`, same shape.
- **MINOR bump** (4.0.0 → 4.1.0): a new subject and a new message type; nothing
  existing changes. Per `docs/contracts/nats-subjects.md` §5.
- OpenAPI: `GET /api/v1/hardware` → `[ChipStateView]` (the message minus the
  envelope, plus `announced_at`); additive, same MINOR.

## hardware-io

- `Pca9685Device.initialise()` already has every fact; after the RESTART it
  builds a `ChipState` and hands it to a new `Spine.publish_chip_state()`.
  `PiPwmChannel.open()` publishes for its chip once (first channel to open;
  keyed on the chip path). `discover_w1()` publishes for the bus master at
  announce time (`initialised` true means "the bus is present"; there is no
  configuration to do).
- Publication is best-effort like `_publish_state`: a failure is logged, never
  raised into `open()`.
- Publish happens after `open()`, which is after `_build_from_registry`; the
  existing capability announcement stays where it is.

## API

- Registry consumer: subscribe `ALL_CHIPS` (durable `registry-chips`, same
  pattern as capabilities), upsert into a new table `chip_state(id, source,
  instance, initialised, initialised_at, facts jsonb, announced_at)` — one row
  per (source, instance). Alembic migration 0019.
- `GET /api/v1/hardware` lists rows, ordered by source then instance.
- Startup: nothing to replay — `BR_CHIP` is last-value, and the consumer's
  first delivery is the retained state.

## iOS

- Hardware leaf: each board section's header gains a second line from its
  `ChipStateView` — `initialised · 502.7 Hz · INVRT off · 16 channels` for the
  PCA9685; `500 Hz · normal · 4 channels` for Pi PWM; `1 probe` for 1-Wire. A
  board with no chip state yet reads `not initialised — no channel adopted`,
  which is the honest wording: the chip is only brought up when something on it
  is adopted.
- No new screen. `ChannelGroups.Group` gets an optional `state`.
- Re-pin after the backend lands (contracts 4.1.0).

## Order

1. contracts (`ChipState`, subject, tests) + hardware-io publisher + `BR_CHIP`
   — one PR; verify on the hub: `nats` retained message per chip.
2. API consumer + migration 0019 + endpoint — one PR; verify `GET /api/v1/hardware`
   on the hub shows the PCA9685 at pre_scale 12.
3. iOS Hardware leaf — one PR after re-pin.

## Out of scope

- Live re-reading registers on demand (a "refresh chip" button). State is
  published when it changes; the app shows the last published.
- Any change to what `initialise()` writes.
- The FET stage / `bench_verified` — CLAUDE.md's business.

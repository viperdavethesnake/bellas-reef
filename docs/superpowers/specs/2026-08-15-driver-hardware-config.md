# Hardware facts belong in device config, not driver constants

Drafted 2026-08-15 from bench findings the same day. Not yet approved.

The PCA9685 driver spells three properties of the attached hardware as module
constants: carrier frequency, the low-duty floor, and output polarity. None of
them are properties of the chip. All three change when the LED driver, the
wiring, or the chip itself changes, and one of them is now measurably wrong.

CLAUDE.md already ruled the first case: "Frequency lives in device config,
never as a chip default. A driver that inherits whatever the silicon powered up
with has not made a decision." The same sentence applies to the other two. This
spec closes all three at once, because closing one leaves a constant behind
that is coupled to it.

## What the bench measured

Stage 1 ran on the PCA9685 at `0x40` on 2026-08-15, raw `i2cset`/`i2cget`, no
Bella's Reef code in the loop.

| PRE_SCALE | Computed @ 25 MHz | David's meter | Implied oscillator |
|---|---|---|---|
| 11 | 508.6 Hz | **544.7 / 544.8 Hz** | 26 773 094 |
| 4 | 1220.7 Hz | **1307 Hz** | 26 767 360 |

The chip runs about 7.1% fast. Two prescaler values across a 2.4x frequency
span imply the same oscillator, so the error is a constant ratio rather than
something that drifts with the divider. One calibration number per chip is
enough, which is the assumption this whole design rests on and is now measured
rather than hoped for.

Nothing about that is broken. 544.7 Hz sits mid-window and the light will not
care. It matters because of what it says about the fix: a config field that
computes `PRE_SCALE` from a hardcoded 25 MHz produces the same 7% error the
constant did, with more machinery around it. reef-pi's PCA9685 driver has
exactly this flaw, with `clockFreq = 25000000` beside a configurable `Freq`.

Asking for 500 Hz has to give 500 Hz. That needs a measured oscillator value,
not a datasheet one.

## The three constants

From `services/hardware_io/bellasreef_hardware_io/drivers/pca9685.py`:

| Constant | Describes | Changes with |
|---|---|---|
| `PCA9685_PRE_SCALE = 11` | carrier frequency | LED driver model, chip oscillator |
| `snap_duty` threshold `0.08` | the undefined-output floor | LED driver model, *and its frequency* |
| `INVRT_ON = True` | output-stage polarity | wiring between chip and load |

The middle one is the trap. reef-pi's issue #316 records that Mean Well drivers
"allow different levels of dimming (10% to 1%) depending on the control
signal's pwm frequency," so the floor is not merely per-model, it moves with
the frequency. Ship configurable frequency alone and `0.08` becomes wrong in a
new way: the operator changes one value and silently invalidates the other.

`INVRT_ON` carries the worst failure. It encodes an assumption about wiring
that has never been proven on this bench, and getting it wrong means
`PwmLevel(duty=0.0)`, the declared safe state, commands full output. Every
fail-safe drill would pass in software with the tank lit at 100%.

A tell worth naming: each of these constants carries a long comment explaining
which bench measurement produced it. A value that needs its provenance
documented in a comment is a value that wants to be data. The comment is a
schema field trying to exist.

## Design

Per-device, in the existing `binding` JSON, beside `{"channel": "0"}`. That
column is already driver-specific and already the place a device's physical
facts live.

### PCA9685 binding

```json
{
  "channel": "0",
  "pwm_hz": 500,
  "min_duty": 0.08,
  "invert": true,
  "osc_hz": 26773094
}
```

`osc_hz` is the calibration value and is per chip, not per channel. Sixteen
channels on one board share an oscillator, so sixteen devices would each carry
a copy of the same number, and nothing stops them disagreeing. Two options,
and the second is better:

1. Repeat it in every channel's binding and validate agreement at build time.
2. Key it by I2C address in a small `chip_calibration` table, looked up once
   when `Pca9685Device` is constructed.

Take option 2. `Pca9685Device` already exists precisely because frequency and
output mode are properties of the chip rather than the channel, and the
docstring says so: "sixteen channels each believing they configure them
independently is how two of them end up disagreeing." Oscillator calibration
belongs in the same place for the same reason.

That leaves the per-channel binding as:

```json
{ "channel": "0", "pwm_hz": 500, "min_duty": 0.08, "invert": true }
```

### RP1 pi-pwm binding

The kernel takes a period in nanoseconds and there is no prescaler to
calibrate, so this path needs two of the four:

```json
{ "channel": "0", "pwm_hz": 500, "min_duty": 0.08, "invert": false }
```

`invert` maps to the sysfs `polarity` attribute. Bench Stage 1 on CH0 and CH2
measured `polarity=normal` correct, duty 0 reading 0 V at the pin, so the
default for this path is `false` and it is proven rather than assumed.

### Validation

An unclamped frequency field is worse than a constant, because it lets an
operator reach a region the hardware misbehaves in while believing the system
checked. Ranges, with their sources:

| Field | Range | Why |
|---|---|---|
| `pwm_hz` | 100 to 2000 | XLG-AB datasheet window is 100 Hz to 3 kHz. Our bench finding of spurious triggering above 2 kHz at 10 to 15% duty cuts the top. |
| `min_duty` | 0.0 to 0.5 | Above half is not a dimming floor, it is a mistake. |
| `invert` | bool | |
| `osc_hz` | 20e6 to 30e6 | Sanity only. A value outside this is a typo, not a calibration. |

Rejection happens at registration, the same boundary that already refuses an
authoritative actuator without a safe state. A bad value must never reach the
driver and become a silent behaviour change.

### Frequency math

```
pre_scale = round(osc_hz / (4096 * pwm_hz)) - 1
```

Then assert the readback equals the computed value, keeping the existing check
but comparing against a computed number instead of a hardcoded 11. The reason
the assert exists does not change: PRE_SCALE is only writable while SLEEP is
set, and a write to a running chip silently does nothing.

## Migration

Alembic revision, additive.

- `chip_calibration` table: I2C address, `osc_hz`, `measured_at`, note.
- Backfill the three `pwm_hz` / `min_duty` / `invert` keys into existing
  actuator bindings using today's constant values, so behaviour is byte
  identical on the first deploy.
- Seed `chip_calibration` with the measured 26773094 for `0x40`.

Existing devices keep doing exactly what they do now. That is the point: this
change moves where the values live, it does not change any of them.

## Out of scope

**No UI.** These are set at import time and read by hardware-io. A reef keeper
has no reason to tune carrier frequency, and a control reaching past 2 kHz
hands them the spurious-triggering region we specifically measured. If it is
ever exposed, it belongs in an advanced device-setup surface with the ranges
above enforced in the client too, and that is a separate spec.

**No behaviour change.** Every default equals the constant it replaces.

## Open questions

1. **500 Hz may be too low.** Mean Well's own dimming application note advises
   keeping PWM frequency above 1.25 kHz to minimise visual distraction, which
   is well above our pinned 500 Hz. Their XLG-AB datasheet permits 100 Hz to
   3 kHz, and our bench found spurious triggering above 2 kHz, so the band
   satisfying both is roughly 1.25 kHz to 2 kHz. Whether 500 Hz visibly
   flickers on a real fixture is David's observation to make at the bench.
   This spec does not change the value. It makes changing it a config edit
   instead of a deploy.
2. **`INVRT_ON = True` is measurably wrong for the stage on the bench.**
   Stage 1 drove CH0 across six duty points with MODE2 INVRT clear and got a
   linear, correctly-signed response: duty 0 at 0 V, 100% at 3.307 V, worst
   error 0.04 percentage points. Setting INVRT would invert that and put the
   declared safe state at full output. The default therefore wants to be
   `false` for this wiring. It is left unchanged pending David's ruling on
   whether this is the output stage that ships, since the PCA9685 to FET chain
   in CLAUDE.md item 0a is expected to invert and would want the opposite.
   This is the field with the worst failure mode of the three and the strongest
   argument for the whole spec: a value that flips meaning with the wiring has
   no business being a module constant.
3. **Contracts version.** Adding keys to `binding` may need a contracts bump
   (3.8.0) depending on how strictly the payload models validate it. Check
   before implementing.

## What this is not

Not a feature. Nothing here does anything the system does not already do. It
moves three hardware facts out of Python and into the registry, where the
capability and device split already puts every other hardware fact, and adds
the one measured number that makes the frequency field honest.

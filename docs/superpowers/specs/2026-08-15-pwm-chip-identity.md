# The factory must resolve the PWM chip by identity, not by index

Drafted 2026-08-15 from a hardware finding the same day. Not yet approved.

hardware-io resolves the RP1 PWM0 block two different ways in two different
places. Discovery does it by hardware identity. The factory does it by
assuming `pwmchip0`. They agree today by luck, and the failure when they stop
agreeing is that adopting a light drives the CPU fan.

## The two halves

**Discovery, correct.** `capabilities.py` has `find_pwm_chip()`, whose
docstring is unambiguous: "Locate the RP1 PWM0 chip by hardware identity, never
by index." It walks `/sys/class/pwm`, resolves each entry's `device` symlink,
and matches on both the block name (`RP1_PWM0_DEVICE = "1f00098000.pwm"`) and
the device-tree compatible (`raspberrypi,rp1-pwm`). Returns `Path | None`.

**The factory, not correct.** `factory.py:120`:

```python
PiPwmChannel(int(binding["channel"]), assignment.device_id, sysfs=sysfs)
```

No `chip_root=`, so it takes the default from `pipwm.py`:

```python
PWM_CHIP_ROOT: Final = Path("/sys/class/pwm/pwmchip0")
```

That constant's own comment says the index "has moved between kernel releases
before" and that "a caller that cares should pass its own." The one caller that
cares does not pass its own.

## Why it matters on this board

Measured 2026-08-15 on the target:

```
/sys/class/pwm/pwmchip0  device: 1f00098000.pwm   <- RP1 PWM0, header pins, ours
/sys/class/pwm/pwmchip1  device: 1f0009c000.pwm   <- RP1 PWM1, fan header
```

From `/sys/kernel/debug/pwm`, PWM1 channel 3 is claimed by `cooling_fan` and
running: 12225/41566 ns, inverse polarity, about 29% at 24 kHz, with the fan at
1203 RPM and the CPU at 58.4 degC. Channels 0, 1 and 2 of PWM1 are unclaimed.

If a kernel update renumbers the chips, discovery keeps announcing PWM0's four
channels correctly, because it resolves by identity. The factory then opens
those channel numbers on `pwmchip0`, which is now the fan block. Nothing raises.
An operator adopts a light and commands the thermal system.

Two things reduce the blast radius, and neither is a control:

- PWM1 channel 3 is held by an in-kernel consumer, so a sysfs export of it would
  most likely be refused as busy. **Unverified.** Testing it means exporting a
  channel that is actively cooling a running CPU, which is not a test worth
  running on hardware in use.
- PWM1 channels 0 to 2 are unclaimed and would export cleanly. Whether they
  reach any pin on this board is unmeasured; CLAUDE.md's mux table records only
  PWM0's four channels reaching header pins.

A kernel consumer does **not** appear as a sysfs export, which is what makes
this quiet. `ls /sys/class/pwm/pwmchip1/` shows no exported channels and reads
as "free". Only `/sys/kernel/debug/pwm` shows real ownership.

## Not host-type detection

The obvious framing is "detect a Pi 5 and refuse to touch the fan." That is a
weaker guarantee than what already exists. Board model tells you which hardware
is present. It does not tell you which `pwmchipN` index the kernel assigned on
this boot, and the index is the thing that varies. Identity resolution answers
the actual question, is already implemented, and is already trusted by the half
of the service that announces channels to the operator.

Detecting the board would also need maintaining as a list. Identity needs
maintaining as one constant that is already there.

## Design

**1. The factory resolves, and refuses when it cannot.**

```python
chip = find_pwm_chip()
if chip is None:
    raise TopologyError("RP1 PWM0 block not found; refusing to open a PWM channel")
PiPwmChannel(int(binding["channel"]), assignment.device_id, chip_root=chip, sysfs=sysfs)
```

`TopologyError` already lands in the existing handler, which logs
`assignment could not be built; device skipped` and continues with the other
devices. That is the correct failure direction: no channel opened beats a
channel opened on unknown silicon. It also matches how discovery already
behaves, which logs critical and announces nothing rather than guessing.

Resolve once per build rather than per channel. Four adopted channels should
not walk `/sys/class/pwm` four times, and four resolutions that could in
principle disagree is a worse property than one that cannot.

**2. Remove the index default from the driver.**

Make `chip_root` a required keyword argument on `PiPwmChannel`. A default that
is only safe when the caller remembers to override it is an invitation, and
this one was not accepted. Tests construct the channel directly and will pass a
temp path, which they already do.

If a default is kept for any reason, it must be something that fails loudly
rather than something that silently works on one kernel.

**3. Tests.**

- The factory passes the resolved chip through, not the default.
- The factory refuses to build when `find_pwm_chip()` returns `None`, and the
  refusal is logged and skips only that device.
- Resolution happens once for a build containing several pi-pwm assignments.
- A fixture where `pwmchip0` fronts the fan block and `pwmchip1` fronts PWM0
  builds against `pwmchip1`. This is the regression the whole spec exists for,
  and it is the one test that would have caught it.

## Scope

Backend only. No migration, no contract change, no client change, no behaviour
change on a kernel that enumerates the chips as this one currently does.

## Open question

Whether `PiPwmChannel` should additionally verify the chip it was handed still
fronts `RP1_PWM0_DEVICE` at open time. It is cheap and it closes the gap between
resolution and use, but it also puts board knowledge in a driver that currently
has none, and the driver's whole design is that the caller decides which chip.
Leaning against, and recording it rather than deciding quietly.

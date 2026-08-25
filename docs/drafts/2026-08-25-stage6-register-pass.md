# Stage 6 — all-channel register/mux pass, 2026-08-25

Run solo by Claude at David's direction ("you can drive any channels you
want — nothing is connected that will be affected", 14:46 PDT). No meter in
the loop, so this is a **register- and mux-level** pass, not an electrical
one: it proves every channel accepts the encoding, reads back what was
written, and (for the RP1) that every pin is really muxed — it does not
prove volts at a pin. The standing rule is unchanged: **any channel gets
its meter check the day it is first wired to a real load, before the load
trusts it.** Stage 4 (real light) was skipped the same afternoon by David's
ruling — the Stage 1/2 meter characterization stands.

Both live channels were identified first and never written: RP1 CH0
(`pi-pwm-0`, on the Sundays curve) and PCA9685 LED0 (`pca9685-0`, on
Brining). Both read identical before and after; hardware-io logged nothing
during the run.

## RP1 PWM0 (`1f00098000.pwm`, pwmchip0 this boot)

Pin mux, read live with `pinctrl get 12,13,18,19` before touching anything:

```
12: a0 // GPIO12 = PWM0_CHAN0   (live, untouched)
13: a0 // GPIO13 = PWM0_CHAN1   <- first live mux confirmation for CH1
18: a3 // GPIO18 = PWM0_CHAN2
19: a3 // GPIO19 = PWM0_CHAN3   <- first live mux confirmation for CH3
```

CH1 and CH3 (the two channels Stage 1 never drove): exported, period set to
2 000 000 ns (the pinned 500 Hz), enabled, duty walked through 0 /
160 000 (8%) / 1 000 000 (50%) / 2 000 000 (100%) ns. **Every write read
back exactly.** Both parked at `duty=0, enable=0, period=2000000`.

Closed on the way: CH3's leftover 1 kHz period from the 2026-08-13
bring-up ("that 1 kHz is nobody's decision") no longer exists — the channel
was found unexported after the factory-reset-era reboots and now holds the
pinned 2 000 000.

One trap for the record: sysfs attribute files of a freshly exported
channel are root-owned for an instant until udev applies the `gpio` group —
a write in that window gets `Permission denied`. One second of settle after
`echo N > export` is enough.

## PCA9685 (bus 1, 0x40)

Chip state as found — exactly what the deployed driver leaves:
`MODE1 0x21` (awake, AI), `MODE2 0x04` (totem-pole, INVRT clear),
`PRE_SCALE 0x0c` (= 12, ≈502.7 Hz from the measured 26.77 MHz oscillator).

Cross-check with no stack in the loop: LED0 (live, read-only) held
OFF-count `0x099A` = 2458/4096 = **60.01%** — the Brining schedule's 60%
hold, read at the silicon.

LED1–LED15, each: counted 50% written (`00 00 00 08`), all four registers
read back exact; full-off bit written (`OFF_H=0x10`), read back exact.
**15/15 pass.** All parked full-off.

## What this closes and what it does not

- Closes: per-channel address arithmetic on both chips (register offsets
  LEDn = 0x06+4n; sysfs channel index), per-pin mux on all four RP1
  channels, the CH3 period trap, and the driver-init register state on the
  running hub.
- Does not close: volts at any pin not already metered. Stage 1/2 metered
  RP1 CH0/CH2 and PCA LED0/LED1; everything else is verified to the
  register boundary only, and meets a meter at load-hookup per the rule.
- Still open on the bench: **Stage 5** — the fail-safe drills re-run while
  a channel is actively driven, with the meter watching the probe point, so
  safe-state-on-failure is a measured 0 V rather than a log line.

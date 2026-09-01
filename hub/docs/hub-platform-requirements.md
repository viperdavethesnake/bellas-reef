# Hub platform requirements

What a machine must provide to run the Bella's Reef hub, and how candidate
boards measure against it.

This document exists because we had no such standard. The Raspberry Pi 5 is the
reference platform and everything in `docs/host-setup.md` is written for it, so
"can board X be a hub" had nothing to be answered against except comparison to
a Pi. That is not a specification, it is a habit.

`host-setup.md` remains the procedure for the Pi 5 specifically. This is the
contract any board has to satisfy before such a procedure is worth writing.

## What the hub actually needs

The list is shorter than it looks, because of a design property worth naming.
**Our drivers use generic kernel interfaces, not board-specific ones:**

| Driver | Interface | Board-specific? |
|---|---|---|
| DS18B20 | `/sys/bus/w1/devices/*/w1_slave` | no |
| RP1 PWM | `/sys/class/pwm/pwmchipN` | no, the ABI is generic |
| PCA9685 | `/dev/i2c-N` via smbus | no |

All board knowledge is confined to `capabilities.py` discovery, which resolves
the RP1 PWM0 block by device identity and shells `pinctrl` to read the pin mux.
That is the correct place for it and it is the only place. Porting to another
board means writing a discovery path, not touching a driver.

### R1. Container runtime

**Docker with Compose v2.** Non-negotiable and not a preference: all six
services run as containers under one boot unit, ruled 2026-08-13 and recorded
in CLAUDE.md. There is no host-process deployment path any more; the dev
launchers were deleted, not deprecated.

The board must run arm64 or amd64 images. Our images are built multi-arch for
both.

### R2. Kernel interfaces, by what you intend to control

Not all three are required. Require what the tank needs.

| To control | Needs | Notes |
|---|---|---|
| Temperature sensing | 1-Wire master with `w1-therm` | sysfs only; there is no chardev equivalent |
| Dimmable lighting | **either** SoC PWM via `/sys/class/pwm` **or** I2C for a PCA9685 | see below |
| Anything on I2C | `/dev/i2c-N` with the operating user in a group that can open it | |

**SoC PWM channel count is not a hard requirement.** A PCA9685 supplies sixteen
channels over a single I2C bus, so a board with one hardware PWM channel, or
none, can still drive a multi-channel light. This matters more than it first
appears: it converts the most board-dependent requirement into the least one.
A board that is weak on PWM and adequate on I2C is a viable hub.

### R3. Resources

Six containers: postgres, nats, victoria-metrics, hardware-io, control-engine,
api.

| | Floor | Reference (Pi 5) |
|---|---|---|
| RAM | 1 GB — measured, not guessed: coco (1 GB Pi 5, 2026-08-31) runs all six services plus live telemetry in ~580 MB with ~400 MB headroom. 512 MB does not fit | 8 GB (dev); 1 GB (coco, production) |
| Storage | 16 GB is the practical minimum. Images alone are several GB before any data | 115 GB |
| Cores | 2 | 4 |

Storage is the requirement people underestimate. PostgreSQL holds config, the
dosing journal, calibration and an append-only audit log. VictoriaMetrics holds
all telemetry at whatever `VM_RETENTION` is set to. Both grow, neither is
optional, and a full disk on a controller is an outage.

### R4. OS and toolchain floor

- Linux 6.x or newer.
- arm64 or amd64.
- Python 3.13+ **on the host is not required.** Services run as images and
  carry their own interpreter. The host needs Python only for scripting.
- **libgpiod v2 if GPIO is used at all.** CLAUDE.md forbids sysfs GPIO and
  libgpiod v1. Currently latent: no driver in the stack uses libgpiod, because
  PWM is not GPIO and 1-Wire and I2C have their own interfaces. It becomes live
  the first time a relay driver lands.

Distribution generation drives several of these at once. Debian 13 trixie gives
Python 3.13 and libgpiod 2.2. Debian 12 bookworm gives 3.11 and libgpiod 1.6.
"Old distro" is not one problem, it is the same problem appearing in three
places.

### R5. Clock

Time-driven actuation must be ordered after a synchronised clock, and an
untrusted clock is a fault state. A board with no RTC battery needs the same
three layers the Pi 5 carries: `fake-hwclock` to restore at boot, `chrony` to
correct once the network is up, and `chrony-wait` so `time-sync.target` means
what it says.

An override is a deadline. The API returns 503 rather than compute one from a
clock that is about to step.

### R6. Network

DHCP is fine. The address is expected to change, so the hub must be resolvable
by name: `avahi-daemon` publishing `<host>.local`, which is the same Bonjour
mechanism iOS uses natively. If Docker bridges exist, avahi needs an interface
allowlist or it advertises unreachable bridge addresses.

## Reference platform: Raspberry Pi 5

Everything in `host-setup.md`. Verified host facts are in CLAUDE.md.
Summarised here only for comparison:

```
Pi 5 Model B Rev 1.0, 8 GB   ·   Debian 13 trixie, kernel 6.18, aarch64
115 GB USB/NVMe   ·   Python 3.13.5   ·   libgpiod 2.2.1   ·   docker 29.7.2
4 hardware PWM channels (RP1 PWM0, custom pwm-4chan overlay)
1 usable I2C bus   ·   1-Wire via dtoverlay=w1-gpio-pi5
```

## Evaluated platform: Banana Pi M64

Surveyed read-only on 2026-08-15. **Viable on interfaces, blocked on hosting.**

**This was a documentation exercise. Ruled by David 2026-08-15: nothing is being
changed or built to support this board.** No code, no discovery path, no
packaging, no CI target. It is written down because evaluating a second board is
what turned an implicit standard into an explicit one, and because the next
person to ask "what about board X" should find a worked example rather than
start over.

Read the section below as a measurement, not a plan.

```
BananaPi-M64, Allwinner A64, 4 cores, aarch64
Armbian 23.11.1 bookworm (Debian 12), kernel 6.1.63-current-sunxi64
1.9 GB RAM   ·   7.3 GB eMMC, 4.9 GB free   ·   994 MB zram swap
Python 3.11.2   ·   libgpiod 1.6.3   ·   docker NOT installed
```

### Against each requirement

| | Status | Detail |
|---|---|---|
| R1 container runtime | **FAIL** | Docker not installed. Installable, but it is the entire deployment model missing. |
| R2 1-Wire | PASS | `w1_gpio` + `w1_therm` loaded, `w1_bus_master1` present, enabled by the stock `w1-gpio` overlay. No valid probe attached: the bus enumerates `00-*` entries whose `w1_slave` reads are empty, and a DS18B20 is family `28`. |
| R2 I2C | **PASS, better than the Pi** | `/dev/i2c-0` and `/dev/i2c-1` (mv64xxx), enabled by the stock `i2c0` and `i2c1` overlays. `i2c-2` is HDMI DDC and should be ignored, exactly as the Pi's `i2c-13`/`i2c-14` are. Two further controllers exist and are disabled. |
| R2 SoC PWM | WEAK, see below | One channel. |
| R3 RAM | MARGINAL | 1.9 GB, already using zram swap at idle. |
| R3 storage | **FAIL** | 4.9 GB free. Postgres plus VictoriaMetrics plus six images does not fit with room to grow. |
| R4 OS floor | PASS with caveats | Kernel 6.1 satisfies 6.x. Bookworm pins Python to 3.11 (irrelevant, containers) and libgpiod to 1.6.3 (latent, see R4). |
| R5 clock | UNVERIFIED | Not surveyed. |
| R6 network | UNVERIFIED | Not surveyed. |

### The PWM situation, which is more interesting than one number

`/sys/class/pwm/pwmchip0` reports `npwm=1`, fronting `1f03800.pwm`. But the
device tree has two PWM nodes:

```
pwm@1c21400   status = disabled     the A64's main PWM
pwm@1f03800   status = okay         R_PWM, the PL-port block
```

The enabled one is R_PWM, turned on by a **custom user overlay**:

```
/boot/armbianEnv.txt:  overlay_prefix=sun50i-a64
                       overlays=i2c0 i2c1 w1-gpio
                       user_overlays=bpi-pwm
```

`bpi-pwm.dtbo` decompiles to a single fragment targeting `r_pwm` and setting
`status = "okay"`. Armbian ships no stock PWM overlay for the a64 prefix, though
it does for h5 and h6, which is why a custom one was built. That is the same
situation as the Pi 5, where `pwm-4chan` had to be built with `dtc` on the box
because no stock overlay exposed all four channels.

So **one channel is a device-tree state, not a proven hardware ceiling.**
Enabling `pwm@1c21400` would need another custom overlay, and how many channels
it would then expose is unmeasured. Not guessed at here.

None of that changes the verdict, because of R2: with a PCA9685 on either of the
two working I2C buses, the SoC PWM count stops mattering.

### What would have to change, if anyone ever did this

Hypothetical. Nobody is doing it. Listed so the size of the gap is on record
rather than re-estimated from scratch later.

1. Install Docker with Compose v2, and verify our arm64 images run on kernel
   6.1.63 sunxi64.
2. Solve storage. 4.9 GB is the blocking item. External media, or a much
   smaller `VM_RETENTION` with eyes open about what that costs.
3. Decide whether 1.9 GB is enough for six containers, by measuring rather than
   arguing.
4. Attach a real DS18B20 and confirm a family `28` device enumerates.
5. Write an A64 discovery path in `capabilities.py`. The current one resolves
   the RP1 PWM0 block by identity and shells `pinctrl`, neither of which exists
   here. This is the only code change the port needs, which is the payoff of
   keeping board knowledge in one file.
6. Verify R5 and R6, both unsurveyed.

### The gating question is not on this list

This board is the subject of an active crash investigation. It was reflashed to
eMMC and is being stress tested, and it had eight minutes of uptime when
surveyed. A hub that crashes is worse than no hub, and every item above is
premature until stability is established. Treat that as item zero.

## What is deliberately not required

- **A Raspberry Pi.** Nothing in the stack requires one. The RP1-specific code
  is one discovery function.
- **A specific PWM channel count**, per R2.
- **Python on the host** beyond scripting, per R4.
- **SPI.** Disabled on the Pi 5 by choice, absent on the M64, used by nothing.
- **A display stack.** The Pi 5 has it stripped and the hub is headless.

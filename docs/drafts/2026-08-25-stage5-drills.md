# Stage 5 — fail-safe drills, meter on the pin. Expectations, then the run.

Authored 2026-08-25 ~15:00 PDT, before the run (rehearsal precedent). The
drills passed long ago in logs and container state; this run puts David's
meter on a driven pin so "failed safe" is a measured 0 V. Claude drives
commands (shown before running, per the bench boundary) and reads registers
/ sysfs as the second observer; every meter value is David's, recorded as
given.

## Setup

- Channel under the meter: **pca9685 PWM ch 0** (`pca9685-0`), on the
  Brining schedule's 60 % plateau (10:00–18:00) — stable for the whole run.
- Second leg observed at register level only: RP1 CH0 (`pi-pwm-0`,
  "Meter Check", Sundays curve) via sysfs `duty_cycle`.
- Expected meter at rest: **0.60 × 3.307 V ≈ 1.984 V** at ch 0.
- Safety contract in play: both PWM channels are registered `authoritative`, safe state
  `duty 0.0`, `heartbeat_timeout_s = 15`.
- Known trap honored: containers are never killed via `docker kill` (it
  marks them manually-stopped and the restart policy declines) — process
  death is always signalled to PID 1 from *inside* (`docker exec … os.kill`),
  the same way `scripts/drill-restart.sh` does it.

## D1 — control-engine dies (the heartbeat drill)

SIGKILL to the engine's PID 1 from inside. The engine's heartbeat stops;
hardware-io must notice within 15 s and drive both PWM channels to safe state
**locally** — no spine, no engine, no API in that decision.

Expected: meter falls from ~1.984 V to **0 V within ≤ 15 s + a beat** of the
kill. ch 0 registers read full-off; RP1 sysfs duty reads 0. Docker restarts
the engine (~15 s); it reconverges and slews **from dark** back to the curve
at 0.05/s → meter climbs back to ~1.984 V in ~12 s of slew. Logs show
heartbeat loss → safe state in hardware-io, and `lighting:converge` in the
restarted engine.

## D2 — hardware-io dies (the honest-exposure drill)

SIGKILL to hardware-io's PID 1 from inside. This is the drill with a real
unknown: the process that enforces safe state is the one that died, and
SIGKILL forbids any graceful shutdown path.

Expected — recorded honestly in advance: the pin **holds its last duty**
until Docker restarts the container (~15 s); on startup hardware-io applies
safe state *first* (checkpoint F behavior), so the meter drops to **0 V at
restart**, then the engine's republished state converges it back to
~1.984 V. The measured width of that held-at-last-duty window is the
finding, whatever it is. (A PWM output holding 60 % for ~15 s is a dark-tank
class exposure, not a heater class one — but the number should be on the
record.)

## D3 — NATS outage (the spine drill)

`docker stop bellasreef-nats-1`, hold ~30 s, `docker start`. hardware-io
loses the spine; heartbeat frames stop arriving.

Expected: safe state within ≤ 15 s of the stop → meter **0 V** for the
outage. On NATS return: hardware-io reconnects and republishes safe states
(#68), engine reconverges → meter back to ~1.984 V. No durable contention,
no stuck consumers — the stack self-heals with zero manual steps.

## D4 — power pull (optional, David's call)

Checkpoint F already proved the cold-boot ladder end-to-end (2026-08-24);
re-running it with the meter adds only the visual. Skip unless wanted.

## Pass criteria

Every drill: (1) 0 V measured within the contract window, (2) recovery to
the curve with zero manual intervention, (3) hardware-io/engine logs agree
with the meter, (4) registers/sysfs agree with the meter at each phase.
Any divergence is a finding by definition.

## Run record

Run 2026-08-25 15:03–15:11 PDT. Meter on pca9685 PWM ch 0 (David reading,
every value his, recorded as given); registers/sysfs as second observer.
**All three drills PASS on the pass criteria** — with one method correction
and one finding that needs a ruling, both below.

### Method correction, found before D1 could run

The Setup section's kill method is wrong, and the expectations above inherit
it: **SIGKILL/SIGTERM to PID 1 from *inside* the container's namespace is a
kernel no-op.** The first D1 attempt (`docker exec … os.kill(1, SIGKILL)`)
silently did nothing — no exit, no restart, no drill. The kernel refuses
default-disposition fatal signals to a namespace's PID 1 from inside that
namespace; hardware-io's SIGUSR1 guard works because it installs a handler,
which is exactly the graceful path a kill drill must not take. The engine has
no drill handler at all.

So: process death is drilled **from the host** — `docker stop` (D1, D3) or a
host-side `kill -9` of the container's process (D2). The existing
`docker kill` trap still stands (it marks the container manually-stopped and
the restart policy declines); a host `kill -9` is distinct from `docker kill`
and the restart policy does fire, which D2 measured.

### D1 — control-engine dies (`docker stop bellasreef-control-engine-1`)

- Both channels driven safe at **~29–30 s** after the stop. hardware-io log:
  `safety event reason=heartbeat_timeout, no heartbeat for 30.0s`.
- Meter: **1.984 V → 0 V** at the safety event. ch 0 registers full-off; RP1
  sysfs duty 0 — registers and meter agree at every phase.
- `docker start`: ~7 s to engine startup, then ~10 s slew from dark back to
  the curve. Meter recovered to **1.984 V**. Zero manual steps beyond the
  start.

PASS — but at **30.0 s**, not the ≤ 15 s the expectations state. See the
finding below.

### D2 — hardware-io dies (host `kill -9`, the honest-exposure drill)

- **Exposure window: ~4 s**, pin held at 60 % (meter steady ~1.984 V) — far
  narrower than the ~15 s the expectations allowed, because a host `kill -9`
  triggers the restart policy immediately (RestartCount +1; contrast
  `docker kill`, which would have left it dead).
- On restart: **safe state first** — meter caught the dip to dark (~3 s)
  before the engine's republished state reconverged it to 1.984 V.

PASS. The held-at-last-duty number is on the record: ~4 s at 60 %, dark-tank
class on this bench (nothing connected).

### D3 — NATS outage (`docker stop bellasreef-nats-1`, 44 s)

- Safe state at **~30 s** into the outage (same 30.0 s window as D1); dark
  for the remainder.
- `docker start`: self-heal in **~9 s** — hardware-io reconnected,
  republished safe states, engine reconverged. Zero manual steps, no durable
  contention, no stuck consumers.
- **Register-verified only. The meter word for D3 was never taken** — the
  bench sitting ended before David could confirm the D3 voltages, so D3's 0 V
  and recovery are asserted by registers/sysfs and logs, not by the meter.
  The other two drills' meter/register agreement is the basis for trusting
  the registers here; it is a weaker basis than a reading and is recorded as
  such.

PASS at register level, with the meter caveat above.

### D4 — power pull

Not run. Checkpoint F (2026-08-24) stands as the cold-boot proof.

### FINDING — 30.0 s enforced vs 15 s declared (needs David's ruling)

Every PWM registration declares `heartbeat_timeout_s = 15` (the Setup section
above states the contract as understood pre-run), but the enforced
engine-loss window measured **30.0 s** in both D1 and D3, and hardware-io's
own log names it: `no heartbeat for 30.0s`. Which is intent — is 30 s a
config bug against a correct 15 s contract, or is the declared 15 a stale
constant? No dependent work ships on top of this until ruled (the
measured-vs-documented discrepancy rule).

RESOLVED 2026-08-28: **no discrepancy existed, so there was nothing to
rule.** Traced on the repo and the live hub: `LIGHT_HEARTBEAT_TIMEOUT_S` in
`contracts/python/bellasreef_contracts/messages.py` has been **30.0 since
the commit that introduced it** (`4e4f0db`), both driver registration
defaults (`dimming.py`, `pca9685.py`) are 30.0, and the fresh hub's device
rows declare 30 for both PWM channels. Declared and enforced agree at 30 s.
The "15" this doc stated pre-run was a conflation with
`liveness_timeout_s = 15.0` — each service's *own* event-loop stall guard
(`LivenessGuard` in hardware-io and control-engine), which watches a process
for stalling, not the engine's heartbeat to actuators. The pre-run "≤ 15 s"
expectations in Setup, D1 and D3 above inherit the same conflation and
should be read as "≤ 30 s + a beat"; the measured results pass against the
actual contract. Whether 30 s is the *wanted* window per role remains a
design choice when tighter-class actuators (heater/relay) arrive —
`heartbeat_timeout_s` is per-registration, so nothing blocks that.

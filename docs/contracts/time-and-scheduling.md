# Time & scheduling — v1

**Status:** draft for review · **Owner:** David / Bella's Reef LLC
**Scope:** how schedules, timers, and delays treat time — DST, midnight,
missed events, restarts — and the profile schema fields that must exist *now*
so the lighting-v2 locale feature is an addition, not a migration.

Split throughout: **[schema-now]** fields land in v1 before the profile/config
model calcifies; **[v2-build]** is implemented with lighting v2 and stays out
of v1 code entirely.

---

## 1. Settled foundations (restating, not reopening)

- Per-profile IANA timezone. `zone="UTC"` = stable photoperiod drifting
  against civil time; a named zone = civil time that steps at DST. Both valid
  operator choices, both tested across the DST boundary.
- Midnight wrap interpolates; no step at the darkest hour.
- Clock trust gates command emission and heartbeats. Untrusted clock →
  engine goes silent → hardware-io safes the tank. (No-RTC host fact.)

## 2. The locale model — the killer feature **[schema-now fields, v2-build math]**

"I'm in Los Angeles; run my reef like Bora Bora, mapped to my day — or let me
set my own sunrise."

```
profile = reef locale  (day SHAPE: photoperiod length, seasonal swing,
                        twilight geometry — equatorial reefs have ~20-min
                        twilights, not hobbyist long fades)
        + anchor       (where that shape sits in the operator's day)
```

**`anchor` — required, Literal [schema-now]:**

| anchor | Meaning |
|---|---|
| `clock` | Today's point-based profile. v1 behavior, unchanged. |
| `solar_natural` | Locale's solar day rendered at the same local clock hours in the profile's timezone; drifts seasonally as the real reef does. **[v2-build]** |
| `solar_custom` | Locale's day shape with operator-pinned sunrise time; whole curve translates (evening-viewing case). **[v2-build]** |

**`locale` — optional `{name, lat, lon}` [schema-now]**, required when anchor
is solar. Solar math is a pure function: (lat, lon, date) → sun elevation →
per-channel intensity shape. Blues lead whites through twilight.

**Preset reef catalog** (Bora Bora, GBR, Fiji, Red Sea, …) is **content, not
code** — a curated data file of name/lat/lon/notes. The archived BA
`location-presets` endpoint is the requirements source. **[v2-build]**
Lunar/moon-phase intensity (also in archived BA) belongs to the same v2.

Solar anchors make DST vanish for those profiles: the sun does not observe it.

## 3. Missed events — `on_miss` **[schema-now, v1-implement]**

Hub reboots at 06:58; the dawn ramp began 06:30. The correct behavior differs
by kind, so every scheduled item declares it:

| `on_miss` | Rule | Applies to |
|---|---|---|
| `converge` | On wake, compute now's target and go there (slew-limited, §5). A ramp is state, not events. | Lighting curves; any continuous output |
| `skip` | A missed discrete action **never** fires late; it is skipped and audited. Wire twin of command expiry. | Anything dosing-shaped; future discrete actions |

Lights are `converge`. Dosing-shaped things are `skip`, always — a late dose
is the exact failure the expiry machinery exists to prevent.

## 4. Timers and delays are durations, not schedules **[v1-implement]**

"Off for 30 minutes" (feed mode, maintenance) counts elapsed seconds — immune
to DST and timezone by construction (monotonic clock; wall-clock deadline
persisted only for restart re-arm).

- Overrides persist as deadlines and re-arm on engine start.
- **Lapse-on-wake:** if the engine was down past an override's expiry, the
  override lapses and the schedule resumes (converging per §3). An override
  that outlives its promise is a silent trap.
- Override state is always loudly visible in every client (design brief rule).
- The archived BA override queue (priorities, persistence) is the requirements
  reference; v1 needs a single active override per target, not the full queue.

## 5. Slew limiting **[schema-now knob, v1-implement]**

One global config knob: max Δduty/second. Applies to convergence after
restart, config edits mid-ramp, and override release alike — no visual pop,
no 0→80% slam over livestock, one mechanism for all three causes. Engine-side
(it shapes *intent*); the driver's 8% floor and snap-to-0 remain driver facts.

## 6. Non-goals (v1)

Solar math, preset catalog, lunar cycle, multi-override queues, per-channel
slew rates, weather/cloud simulation (archived BA had it; explicitly v2+).
The schema fields above are the entire v1 cost of the v2 feature.

# Lighting schedules — design

**Date:** 2026-08-19 · **Status:** draft for David's review
**Origin:** brainstorm 2026-08-19 (Kessil-flow research + archive idea-mining;
David's rulings quoted below are from that session)

## The one-line requirement

> "I make a schedule, call it 'This One', map it out with whatever I want from
> midnight to midnight. Then another called 'That One'. Then I pick 1 or more
> PWM channels and assign that map."

Plus requirement #1 for the Lighting screen: **see the lighting curve, the
specific points, and where we are right now in that curve.**

## The bar (why the archive earned an F)

"Nothing firing when it was supposed to, schedules out of sync, it just
plainly never functioned." The design is arranged around killing those two
failure modes by construction:

1. **It fires.** One source of truth (Postgres); the engine re-reads it every
   tick (1 s) — the exact pattern `_reload_overrides` already runs live on the
   hub. No push channel, no cache invalidation, no state to drift. An edit is
   on the wire within one tick, converging under the global slew.
2. **What you see is what's running.** The app's "now" marker plots the
   engine's *published state* (the wire truth line that already exists), never
   a client-side computation. If the engine and the schedule ever disagree,
   the screen shows it instead of hiding it.

Acceptance is a journey test, not a unit test: create schedule → assign →
engine emits the scheduled duty within a tick; edit → new duty within a tick;
unassign → channel converges to dark. On hardware, the deploy gate stays
CI green → deploy-pi.sh → telemetry verified on the wire.

## Scope

**Phase 1 (this spec):** schedule library (named point maps) + assignment to
one or more PWM channels + Lighting screen curve/points/now + editing from the
app. Ruled 2026-08-19: "for phase 1 we need B and A" — library and custom
curves are the same object; a custom curve is a schedule with one user.

**Later, must not be precluded (accommodations named in §8):**
- On-tank preview (compressed-day run). "Phase 1 does not [need it], but we do."
- Generated schedules: real solar day / real lunar cycle for a location of the
  operator's choice, mapped to their clock — the north star ("run my tank like
  a famous reef"). Simple lunar needs nothing: it is just evening points in
  the map.
- One-off effects (cloud, lightning): manually triggered, duration-boxed,
  "you hit it, it overrides the channel, does its thing for the duration you
  pick, then turns off" — i.e. **overrides with a pattern**, not a new layer.

**Out, permanently or until asked:** groups as an entity (multi-assign covers
it), weather-driven dimming from live APIs, effects queues, behavior-type
taxonomies, per-assignment scale/offset, multi-tank.

## Composition law (contract prose, `docs/contracts/time-and-scheduling.md` §7)

Two layers, total order, no third:

```
resting(channel, now) = assigned schedule's duty_at(now)   — else SAFE_DUTY (dark)
output(channel, now)  = override if one is owed, else resting
```

- An override always wins and always ends (duration, expiry, lapse-on-wake,
  every ending audited) — unchanged from today.
- Release/expiry returns to `resting(channel, now)` — the schedule's value *at
  that moment*, which closes the hold-transition spec's deferred
  "resting-state layer". A held channel's "returns to N %" is computable.
- Future effects are overrides whose level is a pattern instead of a constant
  (additive field on the existing override machinery). Future generated
  schedules replace `duty_at` with a per-day computation behind the same
  interface. Neither changes this law.

## Data model (Alembic 0019)

### `lighting_schedules`
| column | type | notes |
|---|---|---|
| id | UUID PK | |
| name | text, unique, 1–64 | the user's; "Bobs French Fries" is valid |
| points | JSONB | array of `{at: "HH:MM:SS", duty: 0.0–1.0}` |
| zone | text | IANA, default `UTC` |
| anchor | text | `clock` only in v1; `solar_*`/lunar values reserved and **rejected at validation**, exactly as `profiles.py` does today |
| locale | JSONB nullable | `{name, lat, lon}` schema-now, consumed by v2 |
| created_at / updated_at | timestamptz | |

Validation (shared, see §5): ≥2 points, ascending unique times, duty ∈ [0, 1],
no microseconds. Midnight-to-midnight; the wrap segment interpolates (no step
at the darkest hour) — the `ChannelProfile` rules verbatim.

### `schedule_assignments`
| column | type | notes |
|---|---|---|
| channel_id | text PK | one schedule per channel; assign replaces |
| schedule_id | UUID FK → lighting_schedules, **ON DELETE RESTRICT** | deleting an in-use schedule is a 409 (the forgetDevice lesson, pre-applied) |
| created_at | timestamptz | |

Naming note: "assignment" collides with the engine's channel-adoption
`AssignmentLedger`. The table and models say `schedule_assignments` /
`ScheduleAssignment` throughout; nothing reuses the bare word.

No migration of `deploy/config/lighting.json`: its only profile (`led-blue`)
has never matched an adopted channel. The file and
`BELLASREEF_LIGHTING_PROFILES` are deleted, not deprecated.

## Wire contracts (bellasreef-contracts 4.0.0 → 4.1.0, additive)

The point-curve model stops being "engine configuration, not a wire contract"
(`profiles.py` docstring) the moment the app can write it. The validation
model moves into `bellasreef_contracts` so API and engine share one source of
truth; the engine's `ChannelProfile` becomes a consumer of it.

### API (audited mutations; paired-client auth, no roles; contracts MINOR)

(As built, and ruled by David 2026-08-23: auth here is the same
`current_client` token gate as every other endpoint — RBAC is a PRD
non-goal (`prd.md` §non-goals), and an earlier draft of this header said
"admin role" without any design behind it. Every mutation writes an audit
row naming the acting client; that is the whole accountability model.)
| | |
|---|---|
| `GET /lighting/schedules` | list (id, name, points, zone, anchor, assigned channel_ids) |
| `POST /lighting/schedules` | create |
| `GET /lighting/schedules/{id}` | fetch |
| `PUT /lighting/schedules/{id}` | full replace (points list Save = whole curve) |
| `DELETE /lighting/schedules/{id}` | 409 if assigned |
| `PUT /lighting/channels/{channel_id}/schedule` | assign `{schedule_id}` — replaces any existing assignment |
| `DELETE /lighting/channels/{channel_id}/schedule` | unassign → channel rests dark |

Audit rows: `schedule.created/updated/deleted`,
`schedule.assigned/unassigned` (channel_id + schedule_id + actor), same shape
as override audits.

Clock trust: schedule CRUD is **not** 503-gated (storing config needs no
trusted clock); emission already is, via the engine tick gate.

## Engine changes (the part that must not fail)

- New `ScheduleStore` in `bellasreef_db` (sibling of `OverrideStore`): one
  query joining assignments → schedules, returning rows the contracts model
  validates.
- `ControlEngine._tick` gains `await self._reload_schedules()` beside
  `_reload_overrides()`: rebuild the `ChannelProfile` list when the read
  differs from the last one (compare by content; a handful of rows at
  hobbyist scale — no etag machinery). On DB error: keep the last good set,
  log once, metric — the overrides pattern.
- `LightingScheduler` gains `set_profiles(profiles)`: swap the profile list
  in place. **No history is cleared** — a changed curve is just a moved
  target (slew converges), an unassigned channel falls into the existing
  synthetic-channel path and converges to SAFE_DUTY, an assigned-while-held
  channel keeps its hold memory. `reset()`/`forget()` semantics unchanged.
- Everything already proven stays load-bearing and untouched: purity of
  `due()`, slew, deadband, arrival step, snap/ramp holds, clock-trust gate,
  tombstone forget, unadopted-channel suppression ("schedule but no
  adoption; holding").
- Metrics: `bellasreef_lighting_schedules` (count),
  `bellasreef_schedule_reload_errors_total`; reload-change logged with
  schedule names.

## iOS (phase 1 surface)

Research-grounded flow (Kessil is the category norm: overview card → full
graph with now → tap point → edit sheet; nobody drags the curve):

1. **Lighting tab card** (existing, extended): current % (wire truth,
   unchanged) + mini day-curve with a **now dot plotted at the wire duty** —
   scheduled curve and actual output visibly diverge when they diverge.
   Held state keeps today's hold UI; "returns to N %" becomes computable
   from the curve at expiry time.
2. **Light detail**: full midnight-to-midnight curve (Swift Charts,
   read-only), points marked, vertical now line, schedule name, next
   transition ("35 % at 19:00").
3. **Schedules screen** (new, under Lighting): the library list; create /
   rename / delete; editor = chart preview (read-only) above a points list
   (`08:00 → 35 %` rows; add, edit via time-wheel + duty field, swipe to
   delete; Save PUTs the whole curve). Assign = channel multi-select on the
   schedule, mirrored by a schedule picker on the light.

Client is generated from the OpenAPI diff, per the locked stack. Drag-to-edit
points, on-tank preview, and D2 Live Activity are named later items, not in
this round.

## Testing / acceptance

- **Contract tests:** schedule model validation (both packages import the
  same model — a test asserts identity), OpenAPI diff is additive.
- **Engine unit tests:** `set_profiles` mid-run (curve edit converges under
  slew; unassign converges to dark; reassign-while-held preserves hold
  memory; no cleared history), DST boundary both zone styles (existing
  profile tests inherited), fixed-clock fire-time table: given schedule X,
  at time T the emitted duty is Y — the "it fires" proof.
- **API tests:** CRUD + 409s (delete-assigned, duplicate name) + audit rows.
- **Journey test (no shared state, loopback containers/CI):** create →
  assign → adopted fake channel receives scheduled duty within a tick →
  edit → new duty within a tick → hold → release returns to curve-now →
  unassign → converges to SAFE_DUTY.
- **On hardware:** deploy, assign a schedule to `pi-pwm-0`, verify the wire
  duty tracks the curve, and the meter agrees at one point (Stage-2 method,
  same probe point). The 8 % snap rule is unchanged and already proven;
  dawn/dusk crossings exercise it daily by design.

## §8 Accommodation ledger (later features, phase-1 cost)

| Later feature | Phase-1 cost | How it lands |
|---|---|---|
| Solar / lunar generated schedules | `anchor` + `locale` columns + validation reject (already designed, `time-and-scheduling.md §2`) | new anchor values, per-day computation behind `duty_at` |
| One-off effects (cloud, lightning) | none | additive `pattern` field on overrides; same expiry/audit machinery |
| On-tank preview | keep `duty_at(instant)` pure (already is) | compressed-clock runner, later spec |
| Simple lunar evening levels | none | just points in the map |
| Groups | none | named channel-set sugar over multi-assign, if one-by-one ever hurts |

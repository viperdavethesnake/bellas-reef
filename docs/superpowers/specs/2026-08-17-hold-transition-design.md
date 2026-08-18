# Hold transition: snap or ramp, per hold

Approved by David 2026-08-17 (brainstorm concluded 16:19 PDT). One feature
across two repos: an override carries how the light should move to it and
away from it — **snap** (one step) or **ramp** (the global slew) — chosen per
hold from the app. The engine honours it on arrival, on re-hold, on re-arm
after a restart, and on release or expiry alike.

Ruled during the brainstorm:

- **Per hold, not per channel or global.** The hobbyist decides at the
  moment: "off for feeding" wants snap; a photo session at 100 % may want a
  fade. No fixed rule gets both, so the toggle rides on the request.
- **The toggle governs both ends of the hold** (option A). A snap hold snaps
  in and snaps out — on manual release *and* on expiry. A ramp hold ramps in
  and out at the global rate. One word, one behaviour; the app shows the
  transition on the active hold so what will happen at expiry is legible.
  Recorded trade-off: a snap "off for feeding" hold that expires pops the
  light back *up* to its resting level with nobody present. That is the
  feed-mode-ends behaviour hobbyists know from Apex/reef-pi, and it is the
  operator's chosen mode. Snapping *down* (release to dark) is harmless
  either way.
- **Ramp rate stays the global `BELLASREEF_MAX_DUTY_DELTA_PER_S`** (0.01 →
  1 %/s). An operator-chosen ramp *duration* is out of scope.
- **Release returns to today's resting target.** With no schedule and no
  resting-state layer yet, that is `SAFE_DUTY` (dark). The resting-state /
  schedule layer is the schedules round, not this one.
- **Storage is a first-class column**, not a key in the `level` JSONB.
  `level` mirrors the wire `PwmLevel`; transition is how the engine moves
  *between* levels, not a property of one, and hardware-io must never see it.

## Motivation

Stage 2 (2026-08-17) drove holds from the iOS Lighting tab through the whole
stack and the numbers matched Stage 1 to the millivolt — but every hold took
seconds to arrive. `LightingScheduler._emit_for` runs every target through
`_limit`, a global slew of 1 %/s (`deploy/compose.yaml`: "a visible fade
rather than a pop over livestock"), and the scheduler receives overrides as a
bare `Mapping[str, float]`, so it cannot tell a held target from a scheduled
one. A 100 → 5 % hold took ~95 s; David called it out twice that day.

The slew is right for a schedule and for an unattended transition. It is
wrong for an operator standing at the tank who just asked for a level. The
scheduler needs to know *what kind of target* it is moving to, and the hold
needs to say so.

## Contract (bellasreef-contracts / API 3.7.0 → 3.8.0, additive)

- `OverrideRequest.transition: Literal["snap", "ramp"] = "ramp"`. The default
  is today's behaviour, so an existing client that omits it is unchanged.
- `OverrideView.transition: Literal["snap", "ramp"]` — required in the
  response; the server always knows it.
- WebSocket `OverrideContext.transition: Literal["snap", "ramp"]` — required,
  same reason. `stream-frames.schema.json` re-exported; `frame_version` stays
  1 (an added field on a server-produced frame is additive).
- `openapi.json` re-exported. The diff is exactly the new field in three
  places plus the version.
- Audit: `override.created` records `transition` alongside duty and expiry.
- **No change to the NATS command contract.** `ActuatorCommand`/`PwmLevel`
  are untouched; hardware-io never sees a transition. The engine is the sole
  slew authority and remains so.

## Storage (bellasreef-db)

- Alembic migration `0018`: `overrides.transition VARCHAR(4) NOT NULL DEFAULT
  'ramp'`, `CHECK (transition IN ('snap', 'ramp'))` (named constraint).
  Existing rows backfill to `ramp`. Downgrade drops the column.
- `Override` model gains the mapped column.
- `ActiveOverride` gains `transition: Literal["snap", "ramp"]` (default
  `"ramp"` on the dataclass so existing constructor sites in tests keep
  working, but every store read sets it explicitly from the row).
- `OverrideStore.create(..., transition="ramp")` writes it;
  `load_active`, `list_active`, `active_for` select and populate it.

## Engine semantics (bellasreef-control-engine)

### The interface between app and scheduler

`LightingScheduler.due(now, overrides: Mapping[str, HeldTarget] | None)`,
where

```python
Transition = Literal["snap", "ramp"]


@dataclass(frozen=True, slots=True)
class HeldTarget:
    duty: float
    transition: Transition
```

lives in `scheduler.py`. `ControlEngine._tick` builds
`{t: HeldTarget(o.duty, o.transition) for t, o in self._held.items()}`. The
bare-float mapping is removed, not kept as an alternate form — one call site,
one type, and `mypy --strict` finds every test that still passes floats.

### The rule

**A target that comes from a hold moves the way that hold says.**

- `transition == "snap"`: the intent's duty *is* the target, in one step,
  regardless of the configured slew. Reason `hold`.
- `transition == "ramp"`: unchanged from today — `_limit` applies, reasons
  `initial` / `converge` / `ramp` / `refresh` as before.
- Re-holding at a new duty (supersede), and the engine re-arming a hold after
  a restart (cold start with `_last_duty` empty), both follow the *current*
  hold's transition. A snap hold re-armed after a restart jumps from
  `SAFE_DUTY` to the held duty in the first tick; a ramp hold converges as it
  does today.
- Deadband and refresh are unchanged. A snap hold at target still refreshes
  every `refresh_s` like any other level.

### Release and expiry

The scheduler cannot see *why* a hold ended (`release_reason` stays an API/
store concern), and it does not need to: it needs to know how the hold that
just ended said it moves. `LightingScheduler` keeps
`_last_hold: dict[str, Transition]`. `due()` stays pure: every intent it
returns carries `hold: Transition | None` (the transition of the hold it was
emitted under, or `None` when the channel is not held), and `mark_emitted`
records or clears `_last_hold` from that — so a hold whose intent was never
published is never remembered. A hold's arrival, and any change of
transition while held, always produces an intent (reason `hold`) even inside
the deadband, so the memory is exact whenever a channel is held. It is
consulted on the first tick the channel is **no longer held**:

- last hold was `snap`: emit the resting target (profile curve, or
  `SAFE_DUTY` for an unprofiled channel) in one step, reason `release`;
  `mark_emitted` clears the entry (any intent emitted while not held does).
- last hold was `ramp` (or there is no entry): today's path — `_limit`
  toward the resting target, reasons `converge` / `ramp`.

The entry is also cleared by `forget(channel_id)` (tombstone) and `reset()`
(clock-trust gap), for the same reasons `_last_duty` is.

A snap hold superseded by a ramp hold arrives by ramping (the new hold's
mode) and `_last_hold` becomes `ramp`; a ramp hold superseded by a snap hold
jumps and `_last_hold` becomes `snap`. Superseding is "the current hold's
transition", the same rule as arrival.

### Interaction with the <8 % band

Untouched. hardware-io snaps sub-8 % duty to 0 (CLAUDE.md item 3) whatever
the engine's transition; a snap hold at 5 % lands as 0 % exactly as Stage 2
measured. A ramp release from 8 % to 0 still steps 8 → 7 (→ 0 at the pin).

### Logging and metrics

The `command published` log line already carries `reason`; `hold` and
`release` join `initial` / `converge` / `ramp` / `refresh`. No new metric.

## Client (bellasreef-ios — separate PR, after the backend deploys)

- Regenerate the client from the 3.8.0 `openapi.json`; regenerate frame
  types from `stream-frames.schema.json`.
- Lighting tab: a segmented control **Snap | Ramp** beside Hold. The choice
  persists in `@AppStorage`; first-run default **snap** (the complaint that
  started this, and the bench has no livestock). Sent as
  `OverrideRequest.transition`.
- The active-hold row shows the transition ("Snap · 28 min left") from
  `OverrideContext.transition`.
- Nothing else on the tab moves. Slider-is-a-proposal UX and the resting-
  state model stay where they are (queued).

## Testing

**Scheduler (`services/control_engine/tests/test_scheduler.py`,
`test_overrides.py`):**
- snap hold: first `due()` returns one intent at the held duty, reason
  `hold`, with a slew configured that would otherwise take many ticks;
- ramp hold: unchanged — slews at the configured rate, reasons as before;
- snap release: after the hold disappears from the mapping, the next `due()`
  returns the resting target in one step, reason `release`, and subsequent
  ticks are quiet until refresh;
- ramp release: still slews to resting;
- supersede snap → ramp and ramp → snap follow the new hold's mode;
- restart re-arm: cold scheduler, snap hold present → one intent at target,
  not `initial`-from-`SAFE_DUTY`-slewed;
- `forget()` / `reset()` clear `_last_hold` (a channel forgotten mid-hold and
  re-adopted does not snap-release on the strength of a dead entry);
- profiled channel with a snap hold: arrival jumps, release jumps to the
  *curve* value, not to `SAFE_DUTY`.

**Store (`db` tests):** round-trip `transition` through `create` →
`load_active` / `list_active` / `active_for`; a row inserted without the
column value reads back `ramp` (backfill default); migration 0018
upgrade/downgrade in the existing migration test.

**API (`services/api/tests`):** request omits `transition` → stored and
echoed as `ramp`; request `snap` → view and frame carry `snap`; invalid value
→ 422; audit row carries it; `openapi.json` and `stream-frames.schema.json`
regenerate with exactly the expected diff (`scripts/check.sh` already diffs
the exported artifacts).

**Bench (Stage 2 shape — David's meter, same probe points as Stage 1/2):**
- Snap Hold 0 → 100 % on Light 0 (`pi-pwm-0`, pin 32): 3.308 V within one
  engine tick (~1 s), not ~100 s. Release: 0 V within one tick.
- Ramp Hold 0 → 100 %: still ~100 s to 3.308 V (the global slew is intact).
- Snap Hold at 5 %: 0 V (the driver's snap-to-0 still applies).
- Same three rows on Light 1 (`pca9685-0`, LED0) — expected to agree, as
  every Stage 2 row did.

Then the deploy gate as always: CI green → `scripts/deploy-pi.sh` →
telemetry on the wire.

## Out of scope, named

- Operator-chosen ramp duration per hold.
- Resting-state layer (`resting_duty` / schedule / dark) and any schedule
  work.
- Making the slider's "not sent until Hold" unmistakable (queued UX item).
- Items 2–4 of the 2026-08-17 follow-up list (`open()` in the
  `ActuatorDriver` Protocol; chip state on the wire; the adoption-restart
  `failed to publish actuator state` warning).

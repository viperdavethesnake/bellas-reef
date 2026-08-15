# Lighting manual control: hold any adopted light, from the app

Approved by David 2026-08-15 (brainstorm concluded 15:11 PDT). One feature
across two repos: the engine honors overrides on adopted-but-unprofiled
channels, and the iOS Lighting tab becomes a real control surface for manual
holds. Day curves (drag-to-edit schedules, an API-managed schedule contract)
are explicitly out of scope — this spec's scope is "command a duty, for a
duration, honestly."

Ruled during the brainstorm:

- **Manual control first.** The overrides API is the only command path today
  and is sufficient. No new endpoints, no contract bump — contracts stay 3.7.0.
- **The slider is 0–100%, no artificial floor.** The 0–8% dead band is the
  dimmer's property; the driver snaps sub-8% commands to 0 (session-4 ruling)
  and the UI states that in one quiet line rather than restricting the range.
- **Hold duration is presets + custom**: 15 min / 1 h / 4 h / 8 h / Custom,
  capped by the target's `max_runtime_s`.

## Motivation

The app renders lighting state ("Held at X% · remaining", the duty bar) but
cannot command a duty at all — the Lighting tab is a placeholder. And the one
command path that exists is silently dead for exactly the channels an operator
adopts: `LightingScheduler.due()` iterates configured profiles only, so an
override held for a profile-less channel (every channel adopted through the
app today) is stored, re-armed after restarts, expired on schedule — and never
once emitted. Verified 2026-08-15 against `scheduler.py:85-130` and
`app.py:382-383` (engine): `held` is consulted only inside the profile loop.

Bench facts this design is built on (all measured 2026-08-15, recorded in
CLAUDE.md):

- Sub-8% duty is undefined on the XLG dimmer; the driver snaps it to 0. A
  dawn/dusk ramp crosses the band daily — it is the normal path, not an edge.
- Duty 0 is the declared safe state and measures 0 V at the pin
  (`polarity=normal` proven on RP1 CH0/CH2).
- Commanded duty is linear to output within 0.04 percentage points, so a
  linear slider is honest.
- An override is a deadline (`duration_s`, clock-gated 503 when the hub's
  clock is untrusted) on an authoritative actuator (heartbeat 30 s, max
  runtime 18 h). Nothing the screen sets is permanent, and the UI must not
  imply otherwise.

## Feature 1 — engine: overrides on adopted, unprofiled channels (backend)

### Semantics

- A held override targeting a channel with **no configured profile** behaves
  as if that channel had a constant schedule of `SAFE_DUTY` (0): while the
  override is owed, the target is the held duty; when it expires or is
  released, the target falls back to 0. Slew limiting applies in both
  directions, exactly as for profiled channels ("an override ending is just
  another target change").
- Mechanism: `LightingScheduler.due()` iterates the union of configured
  profiles and held-but-unprofiled channels, the latter with a synthetic
  constant-0 schedule. No second code path — the existing
  override-outranks-schedule, slew, deadband, and cold-start logic all apply
  unchanged.
- A synthetic channel keeps emitting after release until it has converged to
  0 (the existing `converging` machinery), then goes quiet. It does not
  persist between engine restarts beyond what override re-arming implies: a
  live override re-arms and re-synthesizes; a dead one does not (hardware-io
  asserted safe state at its own startup, so quiet is truthful).
- Adoption gating is unchanged: intents for unadopted channels are suppressed
  (engine `app.py:385`). The API already 409s `observe_only` targets; targets
  that are not adopted actuators produce intents that the gate suppresses,
  same as today.

### Not changing

- The wire contract (`POST /api/v1/overrides`, `releaseOverride`,
  `listOverrides`) — untouched, still 3.7.0.
- Profiled-channel behavior — byte-identical.
- hardware-io — no changes; `snap_duty` (sub-8% → 0) already lives in the
  driver layer.

## Feature 2 — iOS: the Lighting tab (bellasreef-ios repo)

Replaces the "Not built yet" placeholder.

### Layout

- A card per adopted `light`-role actuator (from the device registry the app
  already holds), named by display name.
- Each card: the hub-reported current duty (wire truth from the state stream —
  never the slider's local position), a 0–100% slider, a duration menu
  (15 min / 1 h / 4 h / 8 h / Custom numeric entry;
  choices above the target's `max_runtime_s` are not offered), and a **Hold**
  button that posts the override.
- An active hold shows the existing "Held at X% · remaining" language plus a
  **Release** button (`releaseOverride`). Remaining time counts down.
- Empty state names its emptiness: "No lights adopted — adopt a PWM channel
  under System."

### Truth rules

- One quiet footnote per card group: "Below 8% this dimmer is off." The
  slider is not restricted; the driver's snap is the behavior and the app
  reports the hub's state, so a 5% hold honestly renders as dark.
- Never implies permanence: remaining time is always visible on an active
  hold; when a hold ends, the card shows the light slewing dark.
- Clock-untrusted 503 renders as its own amber state ("The hub's clock is not
  trusted yet — holds need a deadline."), distinct from generic failure.
- §7.1 states throughout (idle / submitting / active-hold / failed); errors
  amber via `HumanError.describe`; red only for nothing here (Release is not
  destructive — it returns the light to its resting state).

### Not changing

- TankView's existing state rendering (the Lighting tab is the control
  surface; Tank remains the monitor).
- The generated client (all three override operations already exist in the
  3.7.0 client).

## Testing

- **Engine (TDD)**: unit tests on `LightingScheduler.due()` — held unprofiled
  channel emits (with slew from safe start); expiry/release slews back to 0
  and goes quiet after convergence; profiled channels unaffected by the union
  logic; unadopted suppression still holds (app-level test if that gate is
  tested there today).
- **iOS**: kit tests for the card's pure state mapping (registry + stream
  frame + active override → card state) and duration-cap logic; UI states per
  §7.1. No new networking abstractions — the existing HubClient override
  wrappers (or minimal additions in its established idiom).
- **Acceptance is bench Stage 2, run by David**: command 0 / 8 / 50 / 100% on
  "Meter Test" (pi-pwm-0, CH0, header pin 32) from this screen and meter
  against the 2026-08-15 Stage 1 numbers (0 V / 265 mV / 1.654 V / 3.309 V).
  A 7% hold must meter 0 V — that row exercises `snap_duty` through the full
  stack. Any divergence from Stage 1 is a bug by definition.

## Out of scope

- Day-curve schedules and any API-managed schedule contract (own spec, later).
- PCA9685 discovery and `INVRT_ON` (parked on David's output-stage ruling,
  2026-08-15 — see PR #22's record).
- Per-channel ramp-rate configuration (engine default slew applies).
- Perceived-brightness (gamma) mapping — the slider is linear because the
  hardware is; revisit only with a measured basis.

# Bella's Reef — iOS Client UI/UX Review

**Date:** 2026-08-17
**Reviewed build:** iOS simulator (iPhone 17), screenshots captured 09:02 and 17:11–17:12
**Reviewer:** external design review, conversational
**Status:** review only — nothing here is approved work

---

## What this document is

A record of observations from a walkthrough of the shipping UI. It is a **discussion input**, not a
backlog and not a work order.

## What this document is not

- Not a task list. No item here has been accepted.
- Not prioritized for execution. The tiers describe the *kind* of decision each item needs, not a queue.
- Not complete. It is one pass over screenshots by someone who did not have the code at review time.

## How to use it

1. **Propose before implementing.** For any item, the useful next step is a short written proposal —
   what you'd change, what it touches, what it risks — not a diff.
2. **Tier C items should not be implemented at all** without a separate design conversation. They are
   architectural and several of them are still open questions.
3. **Tier E must be answered before Tier C or D are scoped.** Some of those answers will invalidate
   items above them.
4. **One item per PR.** Several findings look adjacent and are not.
5. **Disagreement is a valid response.** Section 8 is where I already know I might be wrong; the rest
   is not exempt from the same treatment.

---

## 1. Established context

Confirmed during review. Recorded so later readers don't re-derive it.

| Fact | Consequence for this review |
|---|---|
| SwiftUI throughout, iOS 26.0 deployment target | No availability guards needed. No UIKit view layer to migrate. |
| Charts are Swift Charts (`AreaMark` envelope, `LineMark` average, `RuleMark` episode bands) | Chart findings are about *unused* framework capability, not about replacing hand-rolled drawing. |
| View layer ≈ 4,000 lines / 11 files | Tier B changes are cheap to land and cheap to revert. |
| Hub has outbound internet; **no inbound**. DHCP address, reachable as `bellasreef.local` via mDNS | Push-out alerting is viable. Cloud-calls-in is not. mDNS already exists on the hub side. |
| Unadopt is a soft flag (`adopted = false`); row, name, thresholds, bindings and history all survive. Readopt is the exact inverse | The data-loss concern I raised at review time was wrong. What remains is a *communication* gap. |
| `forget` is the only hard delete, gated on not-adopted; VictoriaMetrics samples and `sensor_alerts` rows survive it unreferenced | Same. |
| Silence alerting runs unconditionally, independent of thresholds | Changes the shape of finding A1 — see below. |
| Scheduler / photoperiod is **on hold**, deliberately | Out of scope. See section 10. |

---

## 2. Corrections to my initial read

Stated plainly because these were wrong in the conversation that produced this document, and an agent
reading only the findings would otherwise inherit the errors.

**"1 alert episode on a threshold-less sensor is a bug or a stale record."**
It is neither. The row is `alert_class = 'silence'`, raised 21:57:02Z and cleared 23:03:43Z — a real
66-minute w1-bus outage, correctly recorded and correctly closed. Silence episodes are
threshold-independent by design. `bound`, `threshold`, `raised_value` being NULL is the schema working.

There is still a UI finding underneath it (A2), but it is much smaller than what I originally claimed.

**"Unadopt may destroy history, so it needs a confirmation sheet."**
Unadopt destroys nothing and is fully reversible. The finding shrinks to: the UI doesn't say so. (A5)

**"The charts may be hand-rolled and should move to Swift Charts."**
Already Swift Charts. The findings are about capability that exists and isn't wired up. (B4, B5)

---

## 3. What's working — do not regress it

Recorded because reviews that only list problems invite rewrites of things that are already right.

- **The prose.** The clear-margin explanation, "revoking is immediate — the hub checks on every
  request," and "the hub always records Celsius; this only changes what you see" are better written
  than anything shipping in this product category. Any new copy should match this register.
- **Gap disclosure on charts,** with a threshold that scales to the window (`≥45 min` at 7D, `≥3 min`
  at 6H, `45s` at 1H). Almost nothing in this category admits to missing data at all.
- **The adoption model** (announced → adopted → device) is the right abstraction and is visible
  without being intrusive.
- **`alert_class` reaching the client** and being tinted distinctly (`Theme.silence` violet vs
  `Theme.attention`) rather than clients sniffing for NULLs.
- **Contrast as a unit test** (`ThemeTests` asserting ≥ 4.5:1) rather than as a resource nobody checks.

---

## Tier A — Correctness and honesty

*Nature: the UI states something untrue, or fails to state something that affects trust in the data.
Generally small in scope. These are the items where I'd expect least disagreement.*

### A1 · "All clear" doesn't distinguish which alerting is actually running

Tank and Lighting both show a green dot and "All clear."

Given that silence watching runs unconditionally, "All clear" is more defensible than I first assumed —
something *is* being monitored. But with `Yucky Temp` having empty min/max/clear-margin, threshold
alerting is off, and the banner doesn't distinguish that from a fully-configured sensor reading inside
its band. Those are different safety postures shown identically.

*Direction, not prescription:* the status line could reflect coverage as well as state — e.g. distinct
treatment for "monitored and inside band," "silence-monitored only, no band set," and "actively
alerting." Whether that's a third state, a subtitle, or per-sensor rather than global is open.

### A2 · Alert episode summary discards `alert_class`

`History` renders "⚠ 1 alert episode" in `Theme.attention` styling in the summary line, while the chart
band itself is correctly tinted violet for silence. The summary throws away the distinction the schema
went to trouble to preserve.

"1 gap in reporting" and "1 threshold excursion" are different events for the reader. The data to say
which is already in hand.

### A3 · No sample-age or staleness indicator on any live value

If the hub stops responding, `Tank` continues to display `80.8°F` and `All clear` indefinitely. This is
the most consequential item in the document. A controller app that confidently renders a dead sensor's
last value is worse than one that renders nothing.

Related but distinct: there is no connection state (connected / reconnecting / unreachable) anywhere in
the app. One is data age, the other is transport; both are missing.

### A4 · Uncommitted slider values are indistinguishable from committed state

`Light 1` shows `Now 0%` / `Set to 50%`. Nothing marks 50% as a draft. The screen reads as a state
report and isn't one.

Open: whether the right behaviour is snap-to-`Now` on tab return, pending styling on the track, or
both.

### A5 · Unadopt reads as destructive when it isn't

Red, single-tap, adjacent to additive `+` rows, no statement of consequence. Given that it's a
reversible soft flag preserving name, thresholds, bindings and history, the copy is under-selling the
safety of the operation while the styling over-sells its danger.

The reversibility is a genuinely good property. It's currently invisible.

### A6 · 5% is settable on a dimmer with an 8% cutoff

`Light 0` sits at 5% — inside the dead zone documented by the app's own footer. A silent no-op on a
reef fixture is a bad outcome. Whether the fix is clamping, snapping through, or shading the track is
open; the footer text alone isn't carrying it.

### A7 · Duplicated sensor identifier

`ds18b20-28-000000bfe244 · 28-000000bfe244`. The prefix already carries the ROM.

### A8 · Audit log is not usable as a post-mortem record

Four separate issues, likely separable:
- Raw UUID as the primary row identifier rather than the device name
- Actor is `api` for every entry, which defeats the log now that multiple devices can pair
- Relative timestamps only — six rows reading "1h ago" have no recoverable order
- Header copy describes pairing/adoption/revocation; the log is almost entirely override commands

### A9 · Three consecutive "Manual override started" with no intervening "ended"

Either overrides are stacking, or `ended` isn't emitted on supersede. Worth confirming which before
touching the log presentation, since it may be an engine finding rather than a client one.

---

## Tier B — iOS 26 baseline conformance

*Nature: the app is on iOS 26 but is drawing chrome the platform already provides, or leaving
first-party capability unused. Mostly deletions. Low risk, high polish return.*

### B1 · The custom floating tab glyph

The circular tab button occludes content in scrolled states — it covers `channel 10` in one capture and
`channel 2` in another — and is unlabeled and non-standard.

Forward-looking note: iOS 27 (announced June 2026, shipping this fall) re-integrates tab bar search
with navigation, which changes the layout contract custom tab bars were built against. Whatever
happens here, it is probably worth not building this component twice.

### B2 · Nav title legibility over scrolled content

Inline titles blur over noisy content — the "System" header sits directly on top of legible device
rows. `.scrollEdgeEffectStyle` addresses this class of problem directly.

### B3 · `tabViewBottomAccessory`

The strongest single suggestion in this document, and the one I'd most want discussed rather than
implemented from the description.

An accessory strip above the tab bar that persists across tabs and morphs into the bar on scroll would
carry A3 (staleness, connection) globally at the cost of one component, as native chrome rather than
drawn. Something in the shape of `● 80.8°F · 12s ago`, degrading to `⚠ Hub unreachable · 4m`.

This is a real design decision with real cost. It should not be built off a paragraph.

### B4 · Chart capability already available and unused

On Swift Charts already, so these are wiring rather than rebuilding:
- No scrub/crosshair readout (`chartOverlay` + gesture)
- No accessibility chart descriptors — audio graphs would be a genuine differentiator in this category
- Sibling percent charts use different Y domains (Light 0 renders 0–50, Light 1 renders 0–100), so
  they can't be compared by eye. Percent channels arguably want a fixed 0–100 domain.

### B5 · The `AreaMark` min/max envelope is unlabeled

The pale band behind each line is unexplained. Given how good the rest of the copy is, this is a
one-line footer in the existing voice.

### B6 · Empty chart states

7D with ~1 day of data produces an 85% empty plot. `ContentUnavailableView`, or clamping the domain to
available data with a label, both read better than blank axes.

### B7 · Hub addressed by typed IP

`http://192.168.254.236:8000`, against a hub that is on DHCP and already advertises `bellasreef.local`
over mDNS. The address will change. `NWBrowser` discovery with manual IP as fallback closes this.

### B8 · Minor platform affordances

- `.sensoryFeedback(.selection)` on slider commit — currently a slider can silently do nothing (A6)
  with no feedback at all
- `.symbolEffect` on alert state transitions
- 12pt secondary gray on light gray cards is borderline at AA; worth a Dynamic Type XL pass, where
  these rows will also wrap badly

---

## Tier C — Structural

*Nature: these change how the app is organized. **Do not implement from this document.** Each needs a
design conversation, and several depend on Tier E answers. Listed so the shape of the problem is
recorded, not so it gets built.*

### C1 · System tab will not scale

Three adopted devices already produce ~25 rows in one flat scroll, with destructive `Unadopt`
interleaved among additive `+`. The screen holds five concerns with five different growth curves —
hub identity (bounded at 1), paired devices (~5), adopted devices (20–40), available channels
(unbounded, 16 per board), and preferences.

Only the audit log is architected, and it's the only one behind a push.

*One possible direction, offered as a starting point for discussion and not as a target:* System
becomes a fixed-height index — Hub / Devices / Hardware / Alerts / Units / Access / Audit — with
growth pushed into leaf screens.

### C2 · Device and channel are different nouns presented in one list

`Light 1` (user-named, role-bearing, daily) and `pca9685 · channel 7` (board-native, wiring-time,
once) currently share a card. Splitting them would let Hardware organize board → channel, which is
bounded at 16 rows per board forever, and let Devices group by role.

Consequence if pursued: transport identity (`pca9685 · ch 0 · bus 1 · mode1 0x21`) moves to device
detail. Note that `mode1 0x21` was visible in the 09:02 build and dropped by 17:12 — that was the only
surfaced PCA9685 register state, and it's worth relocating rather than losing.

### C3 · Tab bar has no room for future roles

`Tank / Lighting / History / System` spends a top-level tab on one role. Flow, dosing and ATO would
exhaust it. The app's own copy already states the governing rule — *"Controls live on the tab that
uses the device; this list is the inventory"* — so this is about how far that rule extends, not
whether it's right.

### C4 · Adoption is a browse, not a flow

Finding the right channel currently means scrolling near-identical rows and identifying one from
memory. An identify step — pulse the channel, confirm the reading — would use a primitive that already
exists (manual override) to prevent the most expensive class of mistake in the app.

I think this is the highest-value idea in Tier C. It is also the one most likely to be wrong about
effort, since it touches the engine.

### C5 · Alerting has no global home

Thresholds live per-sensor, correctly. Notification routing, quiet hours, mute and escalation are
global and have nowhere to live. Retrofitting this after users have thresholds on eight probes is
harder than establishing the location now — but "establish the location" is not the same as "build the
feature."

---

## Tier D — Capability

*Nature: each is a project, not a change. Scoped separately, sequenced against product priorities, not
against this document. Listed to record what the platform makes possible on an iOS 26 floor.*

### D1 · Alerting architecture — the gating decision

The hub has outbound internet and no inbound. That makes push-out viable and cloud-calls-in
impossible without a tunnel. But **another operator may not give their hub egress**, so alerting has
to degrade rather than depend on it:

| Hub connectivity | Achievable |
|---|---|
| Outbound to APNs | App-closed push. Full alerting. |
| LAN only | No app-closed push. Foreground / background-refresh / in-app only. |

Whichever path is chosen, the app should **state which tier the user is in**. An air-gapped hub that
silently cannot alert is the same class of failure as A3.

`UNNotificationInterruptionLevel.critical` bypasses silent mode and Focus and is the correct level for
a temp excursion, but requires a separate Apple entitlement with lead time. See E4 before starting
that paperwork.

### D2 · Live Activities / Dynamic Island for active overrides

A hold has a start, a duration, a target and needs a cancel — and the app currently offers no
countdown and no release. Lock Screen `Light 1 — 50%, 8 min remaining` with a Release button, via an
App Intent, fits the existing primitive without needing a scheduler.

### D3 · App Intents

Highest leverage single investment if the app grows. Modelling `Device` / `Light` / `Sensor` as
`AppEntity` yields Siri, Spotlight, Shortcuts and Home Screen actions from one implementation, and is
also the substrate D2 and D4 sit on.

### D4 · Control Center / Lock Screen controls

`ControlWidget` for one-tap actions without unlocking.

### D5 · Widgets

Temp and light state at a glance. Note if pursued: widget buttons that write need `ExecutionTargets`
pointed at the main app rather than the widget extension.

### D6 · Correlation view

Temp overlaid with light output on one timeline, with alert episodes marked. The 7D temp swing
(84.8 → ~74 → 81) sits next to light events and the app can't show cause beside effect. All three
series already exist in VictoriaMetrics.

### D7 · Export

CSV/JSON for a time window. For debugging a heater or chiller, more useful than any chart improvement.

---

## Tier E — Open questions

*These change the answers above. Worth resolving before Tier C or D is scoped.*

| # | Question | Blocks |
|---|---|---|
| E1 | Is the "manual override started ×3 without ended" pattern an engine bug or a logging gap? | A9, and the shape of D2 |
| E2 | What is the intended long-term device count — a handful, or dozens? | All of Tier C |
| E3 | Does the hub expose per-device dimmer floors (the 8% cutoff), or is that a client constant? | A6 |
| E4 | Can AlarmKit be fired by an arbitrary remote event, or is it restricted to fixed schedules and countdowns? If the former, it may replace the critical-alert entitlement path entirely | D1 — answer before starting entitlement paperwork |
| E5 | Should threshold values convert on unit change, or be re-entered? The sheet labels thresholds (°F) while the hub stores °C | A-tier if the answer is "convert"; silent conversion error here is a livestock risk |
| E6 | Is `Make primary` in the sensor sheet immediate or Save-gated? Currently ambiguous from the UI | Small, but it's a full-width action row inside a Cancel/Save form |

---

## 4. Considered and set aside

Recorded so these aren't re-proposed as improvements.

| Not recommended | Why |
|---|---|
| Foundation Models / on-device LLM | Nothing here needs generation. A rules engine reads this data better. |
| Image Playground | No use case. |
| Heavy `.glassEffect()` on data-bearing cards | Adopt at the chrome layer and let the SDK do it. Custom glass on content is where the iOS 26 contrast complaints originated, and iOS 27 adds a user-controlled intensity slider that custom glass has to track. Guard anything custom with `accessibilityReduceTransparency`. |
| UIScene lifecycle migration | Pure SwiftUI with `@main` / `WindowGroup`. Not applicable. |
| Migrating charts to a third-party library | Already on first-party Swift Charts. |
| Confirmation sheet on Unadopt as a data-loss guard | Unadopt destroys nothing. The finding is copy, not confirmation. |
| StandBy layouts, App Clips, visionOS | Not until the core is settled. |

---

## 5. Out of scope

- **Scheduler / photoperiod.** Deliberately on hold, to be handled in a separate design session.
  Several capability items that would otherwise appear here — override auto-revert, feed mode,
  revert-to-schedule — depend on it and are excluded for that reason.
- **Backend and engine work**, except where a client finding implies one (A9, E1, E3).
- **Transport security.** The hub is HTTP on LAN. Noted, not addressed here.

Two backend items surfaced during review and are recorded only so they aren't lost:

1. `set_thresholds` clears a band without closing open threshold episodes, and both the evaluator and
   the engine early-return when thresholds are absent. An episode open at the moment a band is cleared
   would be permanently uncloseable. **Not currently reachable** — this hub has no threshold episodes
   at all — but it's a latch bug in the code.
2. `forget_device` performs a bare DELETE with no pre-check against `calibration_records.device_pk` /
   `dosing_journal.device_pk`, both `ondelete="RESTRICT"`. Would raise an IntegrityError rather than a
   clean 409. Not reachable today; latent 500.

---

## 6. Where I already think I might be wrong

Offered directly so it doesn't have to be inferred.

**Channel numbering.** The 17:12 build renumbers PCA9685 channels to 1-based and explains it in a
footer: *"Channels are numbered from 1 here; boards print them from 0, so channel 1 is a board's 0."*

My read is that this is worth reverting — everyone adopting a channel is reading a silkscreen, a
register map, or Adafruit's library, all zero-based, and documenting a translation step usually
signals the abstraction isn't earning itself. The 09:02 build's 0–15 was closer to the hardware.

But this was a deliberate change made after the earlier build, which means there was probably a reason
I can't see from screenshots. If the goal was human-friendly ordinals, an alternative worth weighing is
keeping board-native identifiers and adding a position label at adoption time (`ch 0 · "Left bar"`) —
the user names it once and never reads a channel number again.

Either way: the numeric sort introduced in the later build is an improvement and should survive
whatever happens to the base.

**Tab bar restructuring (C3).** I lean toward collapsing Lighting into a dashboard-first Tank, but
that's an opinion formed without knowing the roadmap, and it costs a tap on the most-used control.

**A1.** I called this ship-blocking before learning that silence alerting runs unconditionally. It's
still a real gap, but it's narrower than I originally argued, and reasonable people could call it a
copy fix rather than a state-model change.

---

## 7. If only three things came out of this

Not a directive — a statement of where I think the value concentrates, for the conversation about what
to actually do.

1. **A3** — staleness and connection state. Converts the app from a remote control into something
   trustworthy while you're out of town. Mostly UI. `tabViewBottomAccessory` (B3) is one plausible
   vehicle.
2. **A1 / A2** — say accurately what is and isn't being monitored, and which kind of event occurred.
3. **B7** — mDNS discovery. The hub already advertises it; the typed IP will break on a lease change.

Everything in Tier C and D is worth more in aggregate and costs an order of magnitude more. None of it
is urgent.

---

## 8. Tier E — answers (2026-08-18)

Answered from the code and the platform docs, not from opinion, so Tier C and D can be scoped
against facts. Written by Claude, ruled by David where marked. Line references are to the commits on
`main` at 2026-08-18 (`10690ba`) and iOS `main` (`65cfc73`).

| # | Answer | Consequence |
|---|---|---|
| **E1** | **Audit gap, not an engine bug.** The DB closes a superseded hold (`db/bellasreef_db/overrides.py:172`, `release_reason='superseded'`) and the engine expires them (`control_engine/app.py:396`), but the audit sink fires only for `override.created` (API, on place) and `override.released` (API, on the manual DELETE). Supersede, expiry and lapse-on-wake write no audit row. | A9 is explained: three "started" is a light re-held twice. Backend fix, small: emit `override.released` with `reason=superseded` from the API create path when it displaces a hold, and from the engine's `publish_audit` on expiry/lapse. Also changes A8's "almost entirely override commands" — endings will appear. D2's shape is unaffected. |
| **E2** | **Pending David's ruling.** Prior from the project record: home hobbyist, one tank; ceiling ≈ 16 channels per PCA9685 board + 4 RP1 channels + a few probes — dozens, not hundreds. | Gates all of Tier C. |
| **E3** | **Client constant, not hub-exposed.** `MIN_USABLE_DUTY = 0.08` lives in hardware-io (`drivers/dimming.py:42`); the app's footer "Below 8% this dimmer is off" is a hand-copied string. Nothing on the wire carries the floor. | A6 may clamp/shade client-side today so long as it reads one constant. The honest fix — the floor as a per-channel fact on the wire (it is a property of the fixture, not of every dimmer) — is a contract item and belongs with the C1/C2 Hardware design. |
| **E4** | **AlarmKit cannot be fired by a remote event.** Per Apple's AlarmKit documentation an alarm is scheduled by the app: `Alarm.Schedule.fixed(Date)`, `.relative(time, repeats)`, or a countdown that starts immediately when no schedule is given. There is no push-triggered path. A silent push waking the app to schedule one is opportunistic and rate-limited by iOS — not acceptable for a temperature excursion. Unverified: whether a Notification Service Extension may call `AlarmManager` at all; assume not until checked. | AlarmKit does **not** replace the critical-alert entitlement path for hub-originated alerts. D1's entitlement paperwork stands. AlarmKit is the right tool only for alarms the app itself sets (a hold-expiry countdown, say). |
| **E5** | **Convert — and it already does, correctly.** Load displays °C in the on-screen unit, save parses back to °C, and the clear-margin is treated as a delta (× 9/5, no +32 shift) both ways (`SensorDetailSheet.swift:159–186`). Preferences are device-local `UserDefaults`, so the unit cannot change under an open sheet from another device; only `.automatic` following a Locale change mid-edit could, and that is negligible. | No livestock-risk conversion bug found. The sheet's `(°F)` label is honest. Nothing A-tier here. |
| **E6** | **Immediate and device-local.** "Make primary" writes `UserDefaults` on tap (`Preferences.primarySensorId` `didSet`), is not Save-gated, and Cancel does not undo it. | The ambiguity is real: an immediate action inside a Cancel/Save form. Presentation fix — move it out of the form or label it as immediate. B-tier one-liner. |

Also recorded from the same pass, so it is not re-derived:

- The two backend items in §5 (threshold-clear latch; `forget_device` bare DELETE) were independently
  found on 2026-08-17 and are unfixed. Both are small backend PRs.
- A6 is half-solved on the wire since #42: `snap_duty` is proven end to end (5 % commanded → 0 V on
  both silicons, CLAUDE.md Stage 2). The reviewer's actual point stands — the slider still lets you
  pick it.
- §6 channel numbering: 1-based display was David's ruling on 2026-08-17 12:11 (wire and bindings
  stay 0-based; the caption warns). The reviewer guessed there was a reason; there was. Whether the
  "position label at adoption" alternative is worth it is a C4-adjacent design question, not a revert.
- Chip state on the wire (PRE_SCALE / frequency / INVRT / initialised, per chip) was ruled 2026-08-18:
  **option A**, a per-chip Hardware surface on the System tab — the backend half of C1/C2, designed
  there. Not a key in the capability `detail` (identity only, per #38), not a field on the device row.

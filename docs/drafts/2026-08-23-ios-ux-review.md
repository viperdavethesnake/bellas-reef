# Bella's Reef — iOS Client UI/UX Review (second pass)

**Date:** 2026-08-23
**Reviewed build:** iOS `main` `b7e8f2e`, backend `main` `f969050`
**Method:** code-level review of the full SwiftUI view layer and the kit models it renders
from (all files under `BellasReef/Views/` and `BellasReefKit/Sources/BellasReefKit/`),
cross-checked against the backend's schedule/chip-state/audit endpoints in
`services/api/bellasreef_api/app.py` and the 2026-08-19 schedules spec — **plus a
screenshot-based visual pass**: David's 23-shot sequential walkthrough on the iPhone 17
simulator (12:02–12:04 PDT today, `bellasreef-ios/samples/`), covering System and all
three leaves, audit log + filter, pairing sheet, History at all four ranges, Lighting,
Schedules, the editor, Tank, and the sensor sheet — all in **light mode**. Findings cite
screenshots by timestamp suffix (e.g. `12.03.43`). No interactive simulator session, so
transient states (drags, dialogs, failures) are still judged from source only.
**Status:** review only — findings, not a work order. Reads as a sequel to
`docs/bellas-reef-ios-ux-review.md` (2026-08-17/18) and its proposals doc; tier names
follow that series.
**Scale judged against:** one operator, paired devices, private LAN, bench Pi with two
adopted lights (`pca9685-0` "Meter Check", `pi-pwm-0`) and one DS18B20. No tank yet.

---

## 1. What shipped since the last review — status ledger

Checked against the 2026-08-18 review and proposals, so neither document has to be
re-derived:

| Prior item | Status at `b7e8f2e` |
|---|---|
| A1 coverage note, A2 episode classes, A4 draft styling, A5 unadopt role, A6 snap-on-release, A7 subtitle, A8 audit rows, A9 release reasons | **Shipped** (#9, backend #48/#54) and visible in the code reviewed here |
| B2 scroll edge, B5 envelope footer, B6 window-coverage caption, B8 haptics/pulse | **Shipped** (#9) |
| B4 scrub + audio graph | **Shipped** (#11) — see P6 below for the one chart that missed it |
| B7 rediscovery | **Shipped** (#10); Wi-Fi-only redeploy verification still open per memory |
| C1/C2 System index, Devices/Hardware/Access leaves, chip state on the Hardware leaf | **Shipped** (#9, #15, backend #61/#62, contracts 4.2.0) |
| B1 floating tab glyph | **Moot** — #16 removed `.tabBarMinimizeBehavior` entirely (iOS 26 layout-loop, documented at `RootView.swift:44–60`). The occlusion symptom cannot recur; the only cost is losing the minimize nicety, which is not a regression worth chasing. |
| Scheduler "out of scope" (§5 of the prior review) | **Superseded** — schedules shipped (#14/#60) and are the main subject of this pass |
| B3 status accessory, C4 identify flow, C5 alerts home, Tier D | Unchanged, still design conversations; not re-reported |

The schedules feature is, overall, well built: the card/detail/library/editor split is
right, hub-authoritative refresh-on-stale-implication (`ScheduleLibrary`) is the correct
concurrency posture for two clients, the mini-curve's "dot leaves the line" is a genuinely
good divergence signal, and the editor pre-validates with the hub's own rules so the
useless wire 422 is never the operator's first feedback. The findings below are mostly
seams between the new surfaces and rules the app had already established elsewhere.

---

## 2. What's working — do not regress it

- **`effectiveHold` precedence** (`LightingCards.swift:182`) — optimistic grant, released-id
  suppression, expiry-by-clock — with the `TimelineView` wrapping the *presence* decision.
  This is the hardest state problem in the app and it is solved and tested.
- **The mini day curve** (`MiniDayCurve.swift`): scheduled line + wire-truth dot is the
  cheapest honest answer to "is it doing what it should", and its VoiceOver label speaks
  both numbers.
- **"No schedule — resting is off."** Absence stated as a state, on both the card and the
  detail. Exactly the register the prior review praised.
- **`ChannelGroups.stateLine` honesty ladder** — `chipStateKnown` distinguishing "asked,
  no row" from "never asked", and facts skipped rather than rendered blank.
- **The editor's seeded dawn-to-dusk template** rather than an empty list two mandatory
  adds away from valid.
- **"Now on Wave — selecting moves it."** in the editor's channel multi-select: the
  replace-semantics stated at the point of decision.
- **Light mode holds up** (whole walkthrough is light appearance): the palette reads as
  the same app — teal accent, calm near-white ground, no contrast surprises anywhere in
  the 23 shots. The contrast suite is doing its job.
- **The prior review's fixes are visibly landed**: `12.03.43` shows "All clear · no
  thresholds set" (A1) and the "not applied yet — tap Hold" draft caption (A4);
  `12.03.40` shows the violet silence band with a violet "1 gap in reporting" phrase
  beside the neutral "1 gap — nothing recorded (≥45s)" (A2) — the two-kinds-of-gap
  distinction reads exactly as designed. History at all four ranges (`12.03.24`–
  `12.03.32`) is legible, labeled, and honest about resolution.

---

## Tier 1 — Must fix

*Correctness, honesty, or an operator dead end. All are small except MF1's full form.*

### MF1 · A schedule assigned to an unadopted channel is invisible, undeletable, and revives silently

**Screens:** `SchedulesView.swift:106–109, 134`, `ScheduleEditorView.swift:233–241`,
`LightDetailView.swift`, `AdoptDeviceSheet.swift`. **Backend context:** assignment
persists per channel by design — "scheduling before adoption is legal, and the engine
holds the curve until the channel is adopted" (`app.py:2343`, spec 2026-08-19); `unbind`
and `forget` do not touch `schedule_assignments`.

**What the operator experiences.** Assign a schedule to a light, then unadopt (or Clear)
that device — a routine on this bench; the registry was wiped this very morning. Now:

1. `SchedulesView` still says "on 1 light(s)", but the Lighting tab shows no such light.
2. Swipe-delete → "It is playing on 1 light(s); the hub will refuse until it is
   unassigned." Tap Delete anyway → refused, error text at the bottom of the list.
3. There is nowhere to unassign it: the editor's channel list filters
   `adopted == true && role == "light"`, the light detail requires a card, and cards only
   exist for adopted devices. Every unassign surface has filtered the ghost out.
4. The only ways out are re-adopting the hardware just to untick a box, or the hub CLI —
   the exact class of dead end the `unbindDevice` docstring exists to close.
5. Worse in the other direction: re-adopting the channel **resumes the curve within a
   tick**, so a freshly adopted light can start driving itself with no adoption-time
   warning — and the Clear dialog's copy ("adopting its channel starts a fresh device")
   is then untrue, because one piece of the old life survives and acts.

**Why it matters at this scale.** Adopt/wipe/re-adopt is the daily loop of bench work
today, and "a light comes on by itself at adoption" is precisely the class of surprise
the safety copy everywhere else works to prevent. This is also the one place the app's
"what is controlling this light and why" story has a hole.

**Direction.**
- Editor's "Assigned lights" section additionally lists assigned-but-not-adopted channel
  ids, marked "not adopted — will play when adopted", untickable off (unassign works on a
  bare channel id; the endpoint doesn't require adoption).
- `AdoptDeviceSheet` checks `ScheduleLibrary.schedule(assignedTo:)` for the would-be
  device id and says "This channel has *Not Interesting* assigned — it will start
  following it when adopted", ideally with an inline unassign.
- Delete dialog names the lights (or channel ids) instead of counting them — see SF8.
- Optional backend half (David's call, not this review's): `forget` clearing the
  assignment would make the Clear copy true again; `unbind` keeping it is defensible and
  matches "reattaches them".

### MF2 · Duty field in the schedule editor has no accessibility label — promote from deferred

**Screen:** `ScheduleEditorView.swift:110–116`.

The deferred-minors memory logged this; assessed here for promotion, and it should be
promoted. The points list is the primary authoring surface of the whole feature, each row
is a `DatePicker` (labeled "Time") next to a bare `TextField("%")` — VoiceOver announces
"text field, 60" with no name, and the adjacent "%" `Text` is a separate element. Every
comparable field in the app carries a label (`lighting-slider` has both label and value).
One line: `.accessibilityLabel("Brightness percent")` (plus `.accessibilityHint` if
feeling generous). The DST wheel item from the same memory entry stays deferred (see
Tier 4).

### MF3 · Audit log failure state renders a raw error dump

**Screen:** `AuditLogView.swift:172` — `load = .failed("\(error)")`.

The one remaining `"\(error)"` in the view layer, in the exact place `HumanError`'s own
doc comment says it used to live ("a full transport trace on a phone screen"). A hub
napping mid-refresh puts an OpenAPI `ClientError` description — operationID, request,
response — into an amber label. `HumanError.describe(error)` is already imported by every
sibling view. One line.

---

## Tier 2 — Should fix

### SF1 · Percent display truncates instead of rounding — a 29% hold reads "Held at 28%"

**Screens:** `LightingView.swift:343, 511, 529, 532` (hold label, truth line, VoiceOver
labels), `TankView.swift:676, 696, 716` (`ChannelRow` duty, hold, spoken label).

`Int(duty * 100)` truncates, and many exact percents are not representable in binary:
`0.29 * 100 == 28.999999999999996`, so a hold placed at 29% renders "Held at 28%"; 57%
renders 56%. The operator sets a number, the hub confirms it, and the app reports a
different one — on a bench where the whole method is comparing displayed percent against
a meter, an off-by-one label is a manufactured discrepancy of exactly the kind the
2026-08-23 acceptance run spent time ruling out ("commanded 20%, measured 0.496 V" was
human memory; this one would be the app). The codebase already does it right in three
places (`returnsToText`, `MiniDayCurve.accessibilityText`, `Dimming.proposalCaption` all
use `.rounded()`). One helper — `percentLabel(_ duty: Double) -> String` — and use it at
every duty-rendering site, with a kit test at 0.29/0.57/0.15.

### SF2 · The 8% floor is communicated on manual holds and nowhere on schedules

**Screens:** `ScheduleEditorView.swift` (accepts 1–7% points without comment),
`ScheduleChart.swift`, `MiniDayCurve.swift` (draw sub-8% as real light),
vs `LightingView.swift:77` ("Below 8% this dimmer is off.") and
`Dimming.proposalCaption`. **Seen at** `12.03.49`: the editor chart draws the curve
through the sub-8% region with nothing marking the band.

A diurnal ramp crosses the 0–8% band twice every day — CLAUDE.md item 3 calls this "not
an edge case; it is the daily path" — and the hub snaps everything under 8% to 0 at the
pin. The manual-hold surface says so twice (footnote + proposal caption); the schedule
surfaces, where the operator will actually author a dawn that lingers at 5%, say nothing,
and the charts draw a gentle 5% glow that will measure 0 V. The operator who learned the
rule on the Lighting tab has no reason to re-derive it inside the editor.

**Direction:** shade the 0–8% band on `ScheduleChart` (a `RectangleMark` in
`Theme.tertiaryText` at low opacity, from the one `Dimming.minUsableDuty` constant), and
one footer line in the editor's points section in the existing voice: "Below 8% this
dimmer is off — points under it will be dark." Snapping the *stored* points is not
proposed: the curve is the operator's intent and the hub owns enforcement; the chart just
must not draw light the pin won't emit.

### SF3 · In-progress vs settled is not communicated for ramps and schedule convergence

**Screens:** `LightingView.swift` (hold row), `LightDetailView.swift`,
`MiniDayCurve.swift`.

A ramp hold to 100% takes ~20 s (global slew 0.05/s); a schedule newly assigned to a
light sitting far off-curve converges over minutes (the acceptance run watched 45→79%).
During that whole window the card says "Held at 100%" while "Now" reads 45%, or shows a
schedule name while the dot sits far off the line — the raw data is honest, but nothing
names the state. "Is it broken or still moving?" is exactly the question a slew invites,
and the operator has to infer the answer from two numbers disagreeing.

**Direction:** when a hold's transition is `.ramp` and `|reportedDuty − hold.duty|`
exceeds the engine's deadband, the hold row reads "ramping to 100% — now 62%" instead of
"Held at 100%"; when a schedule is assigned and no hold is active and the same gap holds
against `curve.duty(at: now)`, a one-line caption under the mini curve: "catching up to
the schedule". Both are pure functions of values the card already has, testable in the
kit. (A settled channel shows nothing new — quiet is the default.)

### SF4 · The light detail chart cannot show the divergence the card can

**Screen:** `LightDetailView.swift:35–45`, `ScheduleChart.swift`.

The card's mini curve plots the wire-truth dot; the full-size chart on the detail screen
— the screen an operator opens *to look closer* — draws the curve and the now line but
not the actual duty. During a hold, a slew, or the sub-8% snap, the detail screen shows
less information than the 44 pt sparkline that led to it. Add a `PointMark` at
(`now`, `reportedDuty`) when a frame exists — `LightDetailView` already resolves the card
live, so the value is on hand. Pairs naturally with SF3's caption.

### SF5 · The editor mixes immediate actions and Save-gated edits without saying which is which

**Screen:** `ScheduleEditorView.swift:229–270` (assign toggles talk to the hub on tap)
vs the curve draft (moves only on Save).

**Seen at** `12.03.53`: the "Assigned lights" section (Meter Check ✓, Light 1) sits
directly under the Save-gated points list with no footer in either direction.

This is the E6 pattern the app already identified and fixed once: `SensorDetailSheet` now
says in copy that "Make primary" is immediate and "not part of Save" (visible at
`12.04.00`, "Show large on the Tank tab" with its applies-at-once footer). The new editor
reintroduces the ambiguity — toggling a light re-points it *now* (and, if the curve draft
has unsaved edits, the light starts playing the *old* curve while the screen shows the
new one), while the points above wait for Save. The create path has a footer ("Save
first, then assign lights to it"); the edit path has none.

**Direction:** a footer on the "Assigned lights" section in the established voice:
"Assignment changes at once; the points above change when you Save." And when the
schedule has assignments, the Save button's consequence deserves one line too — "Saving
changes what N lights are doing now" — since a live edit slews real hardware within a
tick.

### SF6 · Editing a schedule has no Cancel and no unsaved-changes guard

**Screen:** `ScheduleEditorView.swift:159–164` — Cancel exists only when
`schedule == nil` (the create sheet). **Seen at** `12.03.49`/`12.03.53`: the edit screen
carries only a back chevron and Save.

The edit path is a `NavigationLink` push: the back button (or an edge swipe) silently
discards every point edit with no confirmation, and there is no affirmative "discard"
either — just leaving. For a curve that took ten edits to shape, one accidental swipe is
the whole draft. Standard treatment: track dirtiness (draft vs seeded points, name vs
original) and confirm on back when dirty — or present edit as a sheet like create, which
also gets `interactiveDismissDisabled` semantics for free and makes the two paths
consistent.

### SF7 · Schedule audit events render as raw wire tokens

**Screens:** `AuditPhrase.swift` (no schedule cases), `AuditLogView.swift`,
backend `app.py:2366, 2393` (emits `schedule.created/updated/deleted/assigned/
unassigned` since #60).

The fallback-to-raw-action design is working as intended — the events are *visible* —
but the log now shows "schedule.assigned" between fully phrased rows like "Hold on Meter
Check released", for a feature that shipped simultaneously with the audit vocabulary. The
2026-08-23 acceptance run leaned on the audit log as the tiebreaker of record; schedule
rows are now part of that record. Five cases ("Created schedule", "Assigned a schedule to
\(name)", …) plus teaching `AuditRow.subjectId` to read `channel_id` from the payload so
the device name resolves.

**Confirmed visually, and wider than the schedule verbs** (`12.03.06`): the adoption rows
from this morning read "Adopted a device" — no device name — even though the sink writes
`device_id` into the payload. `AuditRow.subjectId` (`AuditRow.swift:16–20`) checks only
the row's `deviceId` column and `payload["target"]`; config events carry the id as
`payload["device_id"]`, so the name never resolves for exactly the bind/unbind/forget
rows A8 most wanted named. One more key in `subjectId`, one test.

### SF8 · The delete dialog offers a Delete it has just predicted will fail

**Screen:** `SchedulesView.swift:94–109, 134`.

When a schedule is assigned, the dialog message says "the hub will refuse until it is
unassigned" — directly above a destructive **Delete** button. Tapping it round-trips to
the hub for a guaranteed 409, whose explanation then renders at the bottom of the list
(possibly off-screen; `problem` is a plain row after the ForEach). Either present the
refusal as the dialog's only content ("Playing on Meter Check — unassign it there
first", no Delete button), or offer the real choice ("Unassign from 1 light and delete",
destructive) which two sequential calls already support. Resolving MF1 makes the
"unassign it from its lights first" instruction actually followable, so these two land
together.

### SF9 · Under a schedule, the "Set to" draft goes stale on its own and nags about it

**Screen:** `LightingView.swift:182, 260` (seeded once from the frame at first
appearance, never re-synced), `Dimming.proposalCaption`. **Seen at** `12.03.43`: Meter
Check reads "Now 88%" while the slider sits at "Set to 82%" with the caption "not
applied yet — tap Hold".

`proposedDuty` is deliberately never overwritten by the stream — right, for a draft the
operator is shaping. But on a *scheduled* light the hub's duty moves on its own, so a
card left open drifts away from its own seed with zero interaction: within minutes the
screen shows a proposal the operator never made, flagged "not applied yet — tap Hold",
which reads as the app asking them to apply something. The A4 fix (say when a proposal
differs) is doing its job on data that is no longer a proposal. The `onDisappear` reset
only helps if you leave the tab.

**Direction:** track whether the operator has touched the slider since appearance (one
`@State` flag, set in the Slider's `onEditingChanged`). Untouched, the draft follows
`reportedDuty` and no caption shows — the card reads as the state report it actually is;
first touch freezes it into a real draft with today's behaviour. This also fixes the
plain-hold case where the seed goes stale during a ramp.

### SF10 · The audit log is mostly "Signed in · hub" — noise, attributed to the wrong actor

**Screen:** `AuditLogView`, `AuditRow.actorName`. **Seen at** `12.03.06`: four of the
eight visible rows are "Signed in / hub · <time>", one per token mint.

Two halves. *Attribution:* the actor renders as "hub" (the `api` mapping) — but the hub
did not sign in; this iPhone did. A `token.minted` event that cannot name the client
undoes A8's "who did what" exactly where credentials are concerned; if the backend can
stamp the minting client's id in the event detail (it knows it), the existing
client-name join does the rest. *Noise:* a mint fires on every reconnect, so at bench
cadence the log's first page is sign-ins with the interesting rows (adoption, holds,
schedule changes) interleaved between them. The category filter already exists; consider
defaulting the view to non-auth categories, or collapsing consecutive sign-ins — either
keeps the record append-only while making the first screen a record of *actions*.

---

## Tier 3 — Polish

### P1 · "on 1 light(s)"
`SchedulesView.swift:108, 120`; **seen at** `12.03.47` ("6 points · on 1 light(s)").
The parenthetical-s in shipping copy, twice, in an app whose prose is its best feature.
`^[\(n) light](inflect: true)` or a ternary.

### P2 · The schedule row counts lights instead of naming them
`SchedulesView.swift:117–121`. At the ruled ceiling (a handful of lights, E2) the names
fit where the count sits: "on Meter Check" beats "on 1 light(s)" in every way at this
scale. Catalog lookup by channel id, fall back to the id.

### P3 · The schedule's timezone is invisible everywhere
`ScheduleEditorView.swift:72, 205` commits `TimeZone.current.identifier` silently on
create and preserves the stored zone on edit; no surface ever displays it. Harmless while
every device involved is in America/Los_Angeles — but zone is the anchor semantics of the
whole feature (the backend acceptance named it), and the day a schedule says 08:00 in a
zone the phone isn't in, nothing on screen explains the offset. One footer line in the
editor: "Times are in \(zone) — the tank's day, not this phone's."

### P4 · Number-pad fields have no Done affordance
`ScheduleEditorView.swift:110` (duty %), `LightingView.swift:557` (custom minutes).
`.numberPad` has no return key; in a Form the keyboard sits over the lower rows until the
operator discovers scroll-to-dismiss. A keyboard toolbar Done (or
`.scrollDismissesKeyboard(.interactively)` on the Form) is the platform answer.

### P5 · Fixed-width fields vs Dynamic Type
Duty field 64 pt, custom minutes 80 pt, transition picker 160 pt. All clip before XL. The
B8 Dynamic Type pass the prior review deferred is still deferred and has grown new
customers; noting so the pass's scope is honest when it happens.

### P6 · ScheduleChart is the one chart without an audio graph
#11's commit title says "audio-graph descriptor on every chart"; #14 then added a new
chart with no `accessibilityChartDescriptor` and no summary label (`ScheduleChart.swift`).
`MiniDayCurve` speaks well; the full-size chart on the detail screen says nothing about
its shape. The `AudioGraph` pattern in `HistoryView.swift:584` transplants almost
directly — a curve is one continuous series with no gaps, so it is the easy case.

### P7 · "channels available" counts 1-Wire probes as channels
`SystemView.swift:154–158`. The Hardware index row sums free capabilities across all
sources, so an unadopted temp probe is a "channel". Split the wording ("2 channels ·
1 probe") or keep the count per the leaf's own grouping.

### P8 · "not initialised — no channel adopted" is PCA9685 vocabulary applied to any board
`ChannelGroups.swift:71`. The fallback fires for any source with no chip-state row.
w1 publishes at announce and pi-pwm publishes at first open, so today the visible case is
a pi-pwm board before any adoption — where "not initialised" is not really the RP1's
failure mode. Per-source fallback copy ("no channel adopted yet" for pi-pwm) keeps the
sentence true per board.

### P9 · The release dialog could name the resting value it returns to
`LightingView.swift:484`: "The light returns to its resting state." The hold row one line
up already computes "returns to 78%" (`returnsToText`); the dialog is where the decision
is made and could say the number — "Release — returns to 78% (its schedule)" / "returns
to off (no schedule)".

### P10 · The 1-Wire group header leaks wire keys, and its empty state says "board"
`ChannelGroups.swift:44–56, 141–153`; **seen at** `12.02.48`: the header reads
"1-Wire bus / **bus_master w1_bus_master1 · family 28** / 1 probe", and below it
"Every **channel** on this **board** is adopted." The PCA9685 header humanises its
shared facts ("address 0x40 · bus 1"); the w1 header prints raw snake_case detail keys,
and the empty-state sentence uses PWM nouns for a bus of probes. Skip or rename the
`bus_master`/`family` keys, and give the w1 group its own empty-state copy ("Every probe
on this bus is adopted.").

### P11 · The temperature probe files under "Other" on the Devices leaf
`SystemView.swift:173` (`$0.role ?? "other"`); **seen at** `12.02.41`: sections "Light"
and "Other", with the DS18B20 under Other. A probe has no role by design, but "Other" is
the app shrugging at its only sensor. Map the nil-role sensor kind to "Sensors" (the
capitalised-raw fallback for unknown roles stays).

### P12 · Schedules live behind an unlabeled toolbar glyph
`LightingView.swift:38–45`; **seen at** `12.03.43` (bare calendar icon, top right). The
library — the only place to create or delete a curve — is an icon-only
`calendar.badge.clock` button. Findable for the operator who built it; a `.titleAndIcon`
labelStyle or a "Schedules" text button costs nothing and removes the one
memorize-the-glyph dependency in the app. Judged polish, not should-fix, because the
assignment flows are also reachable from every light's detail.

---

## Tier 4 — Deferred stays deferred

Assessed each entry of the 2026-08-23 deferred-minors memory on UX grounds, as asked:

| Item | Verdict |
|---|---|
| `timeBinding` anchors to today (DST spring-forward shows a 02:xx point an hour off) | **Stays.** Two mornings a year, display-only, no tank. Fold into the P3 zone-footer work if that happens, since both touch the same binding. |
| `addPoint` at the 23:59 cap can create a duplicate-time dead end | **Stays.** Validation catches it and says why; the chart hiding (below) is the only sharp edge. |
| Editor chart hides entirely while the draft is invalid | **Stays**, with a note: `Section` goes empty rather than showing a "fix the red text below" placeholder. Worth one `ContentUnavailableView`-style line whenever next in the file, not a scheduled fix. |
| Whole-curve PUT re-persists quantization of untouched rows | **Stays.** Single-writer reality; hub normalises. |
| `LightDetailView` computes the card merge twice per body | **Stays.** Bounded at a handful of lights. |
| `ScheduleCurve` minors (`wireTime(86400)`, per-call `Calendar`, DST-fold pin test, self-comparing assertions) | **Stay.** None reachable from the UI. |
| `ScheduleLibrary` refusal-refresh untested | **Stays** (test-side, cheap; not a UX item). |
| `HubClient.hardware()/schedules()` happy-path-only tests | **Stays** (same). |
| Verify delete-`.unknown` / unassign-`.nothingAssigned` refresh on both surfaces | **Verified this pass, by construction:** all three surfaces (`SchedulesView`, editor, `LightDetailView`) route through `ScheduleLibrary`, which refreshes internally on `.deleted`/`.unknown` and `.unassigned`/`.nothingAssigned` (`ScheduleLibrary.swift:79–102`). Entry can be deleted from the memory. |
| Duty TextField accessibility label | **Promoted** → MF2. |

Also still deferred from the prior series, unchanged: B3 status accessory (design,
David's), Dynamic Type XL pass (B8, see P5), C4 identify flow, C5 alerts home, all of
Tier D. History gaining a lighting overlay so schedule effects can be read against
temperature is D6 and stays there.

---

## 5. Open questions (small, this pass's E-tier)

| # | Question | Blocks |
|---|---|---|
| Q1 | Should `forget` clear a channel's schedule assignment (making the Clear dialog's "starts a fresh device" fully true), while `unbind` keeps it (matching "reattaches them")? Backend call, David's. | The shape of MF1's fix |
| Q2 | When the hub's clock is untrusted, holds 503 with pinned copy — what do schedules do, and should the app say anything on schedule surfaces? (Engine treats an untrusted clock as a fault state per CLAUDE.md.) | Nothing today; worth one wire-level answer before the tank |
| Q3 | Does the engine's deadband constant reach the client anywhere for SF3's gap test, or does the client pick its own threshold (1–2%)? | SF3 |

---

## 6. If only three things came out of this

1. **MF1** — the ghost-assignment dead end and silent revive-on-adopt. The one finding
   with a safety flavour, and the one that bites the current bench workflow.
2. **SF1 + MF3 + SF7** — truncated percents, the raw error dump, and the audit rows
   that can't name their device: three smallest-possible fixes that all undermine the
   app's best property, which is that it never lies (and that its log settles disputes —
   it just did, on 2026-08-23).
3. **SF2/SF3/SF4/SF9 as a package** — the schedule surfaces teaching the 8% floor,
   naming convergence, and not manufacturing stale drafts. Together they finish the
   "what is this light doing and why" story the schedules feature started.

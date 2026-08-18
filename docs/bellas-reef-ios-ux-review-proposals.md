# Bella's Reef — UX review: proposals per item

**Date:** 2026-08-18
**Input:** `docs/bellas-reef-ios-ux-review.md` (David's review, 2026-08-17) with its §8 Tier E answers.
**Code audited:** iOS `main` `9f9847f`, backend `main` `d5d4515`.
**Status:** proposals for David's triage. Items marked *built* are on the iOS branch
`ux-review/tier-a-b`, one commit per item, for review — nothing is merged.

## How to read this

The reviewer had screenshots and no code. Several findings describe things the code already
does, or does differently from how the screenshot read. So every item below starts with **what
the code says**, then the proposal. Where the review and the code disagree I say so plainly;
disagreement was invited (review §"How to use it", 5).

Sizes: **S** = one file, under an hour; **M** = a few files, a session; **L** = a design conversation
first.

Recommendations: **accept** (built on the branch, or ready to build), **defer** (real, not now),
**reject** (the code already does it, or the finding does not hold), **design** (Tier C/D shape —
do not build from a paragraph).

---

## Tier A — correctness and honesty

### A1 · "All clear" doesn't say what is being monitored — **accept, S, built**

**Code:** `TankMonitor.statusLine` returns "All clear" whenever the stream is live, no probe is
faulted or stale, and there are no alerts. It does not look at thresholds. `DeviceView` carries
`alertMin` / `alertMax`, so the client can tell "monitored and inside band" from
"silence-watched only, no band set" without a hub change.

**Proposal:** when live and clear, and no reporting probe has both bounds set, the line reads
"All clear · no thresholds set" (attention tone stays teal — nothing is wrong, something is
absent). One probe with a band among several without keeps plain "All clear" for that probe's
sake but the sensor row for the unbanded probe already says so on the sheet.

**Touches:** `TankMonitor` (needs the catalog's threshold view, or a `hasThresholds(sensorId)`
injected), `StatusLine`. Kit test for the three states.
**Risk:** low. Copy only; the tone rule ("red means safety") is untouched.

### A2 · Summary line discards `alert_class` — **accept, S, built**

**Code:** confirmed. `HistoryView` line 376 renders "N alert episode(s)" in `Theme.attention`
regardless of class, while the band (line 220) tints silence violet. The data is on the band.

**Proposal:** count by class: "1 gap in reporting" (violet) / "1 threshold excursion" (amber),
both when both. Same voice as the existing gap-disclosure copy.
**Touches:** `HistoryView` summary. **Risk:** none.

### A3 · No staleness or connection state — **partly reject; the rest is B3**

**Code:** this is largely built and the review missed it because it is invisible until it fires:
- `TankMonitor.isStale` (60 s), `everythingIsStale`, `tone`, `statusLine` ("No data for a
  minute", "Disconnected — …", "Connecting…", "Not connected").
- `StatusLine` and the hero are wrapped in `TimelineView` so staleness re-renders without a
  frame — the comment at `TankView.swift:136` records exactly the failure the review feared.
- A stale hero dims to tertiary and gains an age stamp; a faulted probe shows "Sensor fault",
  never its last number (`TankView.swift:349`).
- The stream client returns on socket close and the monitor sets `.disconnected(reason)`.

So "continues to display 80.8 °F and All clear indefinitely" is not what this build does: after
60 s without a frame it says "No data for a minute" and dims the number; on socket close it says
"Disconnected". What the review is right about, narrowed:

1. **A fresh value has no age.** By design (§7.2: "now" is the default reading of a live
   number). Reasonable — but the reviewer's instinct that a controller should *always* show
   freshness is defensible too. **Design call for David**, S either way.
2. **Nothing is global.** Lighting, History and System do not show connection state at all;
   only Tank does. That is B3's territory — see below.
3. **The 60 s threshold is one number for every probe**, while a probe declares its own
   `pollIntervalS`. Staleness should be a multiple of the probe's cadence, not a constant. **S**,
   worth doing regardless: `isStale` reads `catalog.device(id)?.pollIntervalS`.

**Recommendation:** reject the headline; accept (3) as a small fix; (1) is David's; (2) → B3.

### A4 · Uncommitted slider values look committed — **accept, S, built**

**Code:** confirmed. `LightingView` keeps `proposedDuty` as a draft ("Set to"), separate from
`Now`, and the comment at line 152 says it is a pending choice — but nothing styles it as one.

**Proposal:** while `proposedDuty != now` and no hold is in flight, the "Set to" row reads
"Set to 50% — not applied" in secondary text and the Hold button is the only primary control;
on tab return the draft snaps to `Now` (the stream is the truth; a draft nobody committed is not
state worth keeping across a tab switch). Both halves of the review's "open" are answered: snap
on return *and* pending styling.
**Touches:** `LightingView`. **Risk:** low; snapping a draft the operator was mid-adjusting is
the one thing to get right — only on `onDisappear`, never on a stream frame.

### A5 · Unadopt reads destructive — **partly reject, S, built (the copy half is done)**

**Code:** there *is* a confirmation dialog (`SystemView.swift:134`), and its message already
says the reversible thing: "History is kept — adopting the same hardware again reattaches it."
The review's "no statement of consequence" does not hold for this build. What remains: the row
button and the dialog button both carry `role: .destructive` (red) for an operation the copy
itself calls reversible.

**Proposal:** drop `.destructive` from the row button (plain accent), keep the confirmation,
keep the copy. Red stays reserved for the one hard delete ("Clear this device?" / `forget`).
**Touches:** `SystemView` two lines. **Risk:** none.

### A6 · 5 % is settable on a dimmer with an 8 % floor — **accept, S, built**

**Code:** confirmed. `Slider(value:in: 0...100, step: 1)`; footer "Below 8% this dimmer is off."
The hub snaps sub-8 % to 0 (`snap_duty`, proven end to end at Stage 2), so 5 % is a *dark*
no-op, not an undefined one — but the app still lets you ask for it and shows "Set to 5%".

**Proposal (client, now):** the draft snaps down to 0 when it lands under 8 % on release, and
the "Set to" row says "Set to 0% (below 8% is off)" — the same rule the hub applies, applied
where the operator can see it. The footer stays. **E3 (floor on the wire)** stays a design
item; until then the constant lives in one place in the kit (`Dimming.minUsableDuty = 0.08`)
with a comment naming `services/hardware_io/.../dimming.py:42` as the source.
**Touches:** `LightingView`, one kit constant + test. **Risk:** low; the mapping is the hub's
own rule, so nothing the app shows can differ from what the pin does.

### A7 · Duplicated sensor identifier — **accept, S, built**

**Code:** confirmed. `deviceSubtitle` = `driverId · channel · role`; for a DS18B20 the driver id
is `ds18b20-<rom>` and the channel is `<rom>`.

**Proposal:** when `channel` is a suffix of `driverId`, omit it. `ds18b20-28-000000bfe244 ·
temperature`. **Touches:** `SystemView.deviceSubtitle`. **Risk:** none.

### A8 · Audit log is not a post-mortem record — **accept 3 of 4, S–M; one is done backend-side**

Four separate things, as the review says:
- **UUID as row identity** → device name from the catalog when the event carries a `target` /
  `device_id`, UUID in the detail line. **S, built.**
- **Actor is `api` for every entry** — *code:* the API's sink writes `actor: api` and then
  spreads `detail`, so events that carry `actor` (overrides, pairing) already show the client
  id, not `api`. The rows that show `api` are the ones with no actor in their detail (device
  bind/unbind, thresholds). The fix is backend: pass `actor` on every config event. **S, backend,
  not yet done.** The client half — render a client *name* for a client id — needs
  `GET /clients` joined in the view; **S, built** on the branch (falls back to the short id).
- **Relative timestamps only** → absolute time with the relative in secondary ("14:02 · 1h
  ago"); the log is the one place recency is not the point. **S, built.**
- **Header copy** describes pairing/adoption/revocation while the log is mostly overrides →
  reword to what the log is. **S, built.**

### A9 · Three "started", no "ended" — **resolved by E1 (backend #48, deployed)**

Supersede, expiry and lapse now write `override.released` with a `reason`. The client's
`AuditPhrase` maps every `override.released` to "Manual override ended"; with the reason on the
wire it can say "ended (superseded)" / "expired" / "lapsed at restart". **S, built.**

---

## Tier B — iOS 26 baseline

### B1 · Custom floating tab glyph — **reject the diagnosis; verify the symptom on the sim**

**Code:** there is no custom tab bar. `RootView` uses `TabView` with `Tab(…)` and
`.tabBarMinimizeBehavior(.onScrollDown)` — the iOS 26 *system* behaviour that collapses the bar to
a floating glyph on scroll. That is what the screenshot shows. The occlusion of `channel 10` /
`channel 2` is either the system minimized bar over content whose scroll view is missing a
bottom safe-area contribution, or a `List` inside a `ScrollView` — needs eyes.

**Proposal:** verify with David at the sim (I could not drive the Simulator window this
session). If real: it is an inset bug in `SystemView`, not a component to delete. Do **not**
remove `.tabBarMinimizeBehavior` on the strength of a screenshot — it is the platform behaviour
the review's own iOS 27 note argues for keeping.

### B2 · Nav title legibility over content — **accept, S, built**

**Code:** no `.scrollEdgeEffectStyle` anywhere. **Proposal:** `.scrollEdgeEffectStyle(.soft, for:
.top)` on the four tab roots. **Risk:** none; system modifier.

### B3 · `tabViewBottomAccessory` — **design (David), M**

The review's own words: do not build from the paragraph. The case for it got *stronger* from
the code audit, not weaker: the model already has everything the accessory would show
(`connection`, `lastFrameAt`, primary probe, `isStale`), and today only the Tank tab renders
any of it. A `● 78.7 °F · live` / `⚠ No data for a minute` / `⚠ Disconnected` strip on all four
tabs is one component reading state that exists. Cost is the design decision, not the code.

**Proposal for the conversation:** build it as `TabView.tabViewBottomAccessory { StatusStrip() }`
with three states only (live / stale / disconnected), teal / amber / amber, matching
`HealthTone`; no glass of its own (design brief §7.6). Prototype on the branch behind a flag if
David wants to see it before deciding.

### B4 · Chart capability unused — **split**

- **Scrub / crosshair readout** — accept, **M**: `chartOverlay` + drag gesture, a readout of the
  bucket under the finger (mean, min–max, time). Not built yet; worth its own commit.
- **Accessibility chart descriptors** — accept, **S**: `accessibilityChartDescriptor` with the
  series; audio graphs come free. Not built yet.
- **Sibling percent charts with different Y domains** — **reject as stated**: `yDomain` returns
  `0...100` for any non-temperature trace (`HistoryView.swift:194`, with a comment saying why).
  If the screenshot showed 0–50 the trace was classified as temperature — that would be a
  `isTemperature` bug, not a domain choice. Verify on the sim; if it reproduces, it is S.

### B5 · Envelope unlabeled — **accept, S, built**

One footer line in the existing voice: "The band is each interval's low to high; the line is
its average." Placed with the gap-disclosure caption so the two explanations sit together.

### B6 · Empty chart states — **accept, S, built**

**Code:** `ContentUnavailableView` exists for *not connected* and "Nothing recorded" for an
empty window, but a 7D window with one day of data draws six days of empty axis (the x domain is
deliberately the picked range, `HistoryView.swift:292`). **Proposal:** keep the picked range as
the axis (the review's "clamp to available data" would make 7D lie about being 7D) and add a
one-line caption when data covers < 50 % of the window: "Data starts <date> — the hub has
recorded 1 day of the 7 shown." Honest, and it says why the plot is empty.

### B7 · Hub addressed by typed IP — **reject the diagnosis, accept the consequence, M**

**Code:** discovery is already `NWBrowser` for `_bellasreef._tcp` (`HubDiscovery.swift`), with
manual entry as the documented fallback. What the reviewer saw is the *result* of discovery: the
Bonjour endpoint is resolved to the address actually reached (an IP — see the comment at
`HubDiscovery.swift:206`, and the zone-id stripping below it) and that IP is stored. So the
lease-change break is real, but the fix is not "add mDNS" — it is **re-resolve when the stored
address stops answering**: on connect failure, if the hub was discovered, browse again for the
same service name and swap the base URL. Store the service name alongside the URL.
**Touches:** `Hub`/`remember`, `TankMonitor` connect path, `HubDiscovery`. **Risk:** medium —
touches the connect path; needs the fault-injection test (unreachable stored URL, discovery
returns a new one).

### B8 · Minor affordances — **accept, S, built (two of three)**

- `.sensoryFeedback(.selection)` on slider commit — the code has `.success` on hold success;
  adding `.selection` on the Hold tap itself. Built.
- `.symbolEffect` on alert transitions — built on the status dot (`.pulse` on `.attention`).
- Dynamic Type XL pass — **defer**, real work; the 12 pt tertiary already passes 4.5:1 by test.

---

## Tier C — structural (design conversations, none built)

E2 is answered ("dozens at most; RP1 + one PCA9685 board"), so these can be scoped. Sized for
that answer, not for a rack.

### C1 · System tab will not scale — **design, L; the Hardware leaf is where follow-up 3 lands**

With ~20 PWM channels + a few probes as the ceiling, "will not scale" is bounded — but the
five-concerns-in-one-scroll point stands at today's size (25 rows for 3 devices). Proposal for
the conversation: **System becomes an index** — Hub · Devices · Hardware · Access · Audit — each
a push. **Hardware** is board → channel (bounded at 16 + 4) and is the home of per-chip state
(PRE_SCALE / frequency / INVRT / initialised — David's option-A ruling 2026-08-18). That needs
the backend half: a `ChipState` message on `bellasreef.chip.<source>` (contracts MINOR), API
store + `GET /api/v1/hardware`, iOS view. **Alerts** and **Units** from the review's sketch:
Units is one row and can stay on the index; Alerts has no global content until D1/C5.

### C2 · Device and channel are different nouns — **design, folds into C1**

Agree. Devices (role-bearing, named) and channels (board-native) split when C1 lands;
transport identity moves to device detail; register state to Hardware. This is the same design
conversation as C1, not a second one.

### C3 · Tab bar has no room for future roles — **defer; not until a second role exists**

The governing rule is already stated in the app's copy. Deciding whether Lighting folds into
Tank costs a tap on the most-used control, and there is no dosing/ATO/flow to make room for
yet. Revisit when the second role is real.

### C4 · Adoption is a browse, not a flow — **design, M–L; agree it is the highest-value idea**

An identify step (pulse the channel via a short hold, confirm, name it) uses primitives that
exist: `POST /overrides` with `snap` for a few seconds, then release. Engine-side nothing new;
the risk the review names ("touches the engine") is smaller than feared since #42 — a 3 s snap
hold at 30 % is exactly what the Lighting tab already does. Needs a design conversation about
the flow (System → available channel → *Identify* → name → adopt), not about the mechanism.

### C5 · Alerting has no global home — **design, tied to D1**

Establish the location (an Alerts leaf under C1's index) without building routing. Cheap once
C1 exists; pointless before it.

---

## Tier D — capability projects (recorded, not scoped)

D1 depends on E4's answer: AlarmKit does not replace the critical-alert path, so push-out with
`.critical` interruption level and the entitlement is the route, degrading to in-app when the hub
has no egress — and the app must say which tier it is in. D2 (Live Activity for a hold) is the
one with a natural next step now that holds have a transition and a countdown: worth a design
session on its own after A4/A6 land. D3–D7 unchanged.

---

## Order proposed

1. **Land the S items** on the iOS branch (this document's *built* set), one commit each,
   review together — most of Tier A and half of Tier B, ~a day of review.
2. **David's calls:** A3(1) always-show age; A5's role change; B3 yes/no (prototype offered).
3. **B4 scrub + descriptors, B7 re-resolve** — three M items, one PR each.
4. **C1/C2 design conversation** — the Hardware leaf and chip state, one spec.
5. **C4 identify flow** — spec after C1.
6. Backend smalls alongside: `actor` on config audit events (A8); the two §5 latent bugs.

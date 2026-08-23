# iOS UX Fixes Implementation Plan (batch B + schedule-soon UX)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 2026-08-23 UX review's must-fixes (MF1 ghost schedule assignments, MF2 duty-field accessibility, MF3 raw error dumps) plus the highest-value should-fixes: SF1 percent truncation, SF9 stale slider draft, SF2–SF4 (8 % floor taught in editor and charts, convergence wording, actual-duty marker), and SF7 (schedule.* audit rendering + subject-id resolution).

**Architecture:** Kit-first, per the house rule ("a view never owns logic that could carry a test"): every new decision lands as a pure helper in `BellasReefKit` with a Swift Testing suite, then views call it. Repo: `/Users/david/visualstudio/bellasreef-ios` (separate from the backend repo; work happens there, on a feature branch off `main` @ `b7e8f2e`).

**Tech Stack:** Swift 6.2 / SwiftUI, iOS 26, SwiftPM kit (`BellasReefKit`, strict concurrency complete), Swift Testing (`@Suite`/`@Test`/`#expect`), generated OpenAPI client (never hand-edited), xcodegen (project is gitignored; `project.yml` is source of truth).

**Spec:** `/Users/david/visualstudio/bellasreef/docs/drafts/2026-08-23-ios-ux-review.md` (findings MF1–MF3, SF1–SF4, SF7, SF9) — read it first; this plan implements exactly those.

## Global Constraints

- Kit tests: `cd BellasReefKit && xcodebuild test -scheme BellasReefKit-Package -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -skipPackagePluginValidation`.
- App build gate: `xcodegen generate && xcodebuild build -project BellasReef.xcodeproj -scheme BellasReef -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -skipPackagePluginValidation` (set `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`).
- Do not touch `Sources/BellasReefAPI/` (generated) or `Contracts/openapi.json` — nothing here needs a contract change; MF1's unassign works on bare channel ids against the existing API (verified: backend `unassign_schedule` never touches the registry).
- File headers: match the file being edited (`// Bella's Reef iOS — closed source.` or SPDX pair). Kit files: one concept per file, `public enum` + static funcs for display helpers.
- UI tests (`BellasReefUITests`) are bench-only and CI-excluded — do NOT add to them; testable logic goes in the kit.
- Conventional commits; PR to main; CI green (macos-26 workflow) → merge. No deploy step — the sim picks up main.

## Context a fresh engineer needs (verbatim findings from exploration)

- **SF1 truncating sites (7):** `LightingView.swift:343` (hold label), `:511` (truth line), `:529,532` (a11y), `TankView.swift:676` (row duty), `:696` (row hold), `:716,718` (spoken). Already-correct sites use `Int((x * 100).rounded())` — e.g. `LightingView.swift:499`, `LightDetailView.swift:80`, `ScheduleEditorView.swift:50`, `Dimming.swift:35`. No percent helper exists; `Dimming` (kit) is its natural home.
- **SF9:** `LightingView.swift:176-182` (`@State proposedDuty`, seeded once in `init` at 257-260, re-synced only in `.onDisappear` 443-445). Slider `onEditingChanged` at 379-381; caption via `Dimming.proposalCaption` at 387-397.
- **MF2:** `ScheduleEditorView.swift:110-113` — the duty `TextField("%", text: $point.dutyPercentText)` has no accessibility label/identifier. Precedent: `LightingView.swift:384-386`.
- **MF3 sites (3):** `AuditLogView.swift:172` `load = .failed("\(error)")`; `AdoptDeviceSheet.swift:203` `problem = "\(error)"`; `DeviceCatalog.swift:80` `state = .failed("\(error)")`. `HumanError.describe(_:) -> String` exists (`HumanError.swift:43`).
- **SF7:** `AuditPhrase.swift:23-57` — missing all five `schedule.*` tokens (`schedule.created/updated/deleted` carry `schedule_id`,`name`,`actor`; `schedule.assigned/unassigned` carry `channel_id`,`schedule_id`,`actor`; all category "config"). `AuditRow.swift:16-20` `subjectId` checks only `deviceId` param and `payload["target"]` — misses `payload["device_id"]` and `payload["channel_id"]`. Call site `AuditLogView.swift:106-112` resolves names via `DeviceCatalog.name(for:)` (falls back to raw id).
- **MF1:** editor channel list filters `adopted == true && role == "light"` (`ScheduleEditorView.swift:232-242`); toggle handler `toggle(_:schedule:currentlyThis:)` at 272-294 already unassigns by bare string. `ScheduleLibrary.unassign(channelId:)` (`ScheduleLibrary.swift:98-102`) succeeds for ghosts (backend verified). Delete dialog `SchedulesView.swift:94-109`; refusal message at 134. Adopt sheet computes the proposed id inside `adopt()` (`AdoptDeviceSheet.swift:168-171`: `"\(driverType.rawValue)-\(capability.channel)"` lowercased, spaces→dashes); its confirm dialog is at 129-145; the sheet has `model` in scope but never reads `model.library`. Clear/forget dialog copy at `SystemView.swift:255-258` claims "adopting its channel starts a fresh device" — untrue for schedules. `ScheduleLibrary.schedule(assignedTo:)` exists (`ScheduleLibrary.swift:54-56`).
- **SF2:** `ScheduleEditorView.swift:24-34` DraftPoint accepts 0–100; validation ladder 76-83 has no floor mention; Points Section 100-127 has no footer (footer idiom at 132-137). Kit floor constant: `Dimming.minUsableDuty = 0.08`; copy precedents `LightingView.swift:77` and `Dimming.proposalCaption`.
- **SF2b/SF4:** `ScheduleChart.swift:10-60` — LineMark + PointMark + now-RuleMark; y-domain 0...100; no floor band, no actual-duty mark, callers at `ScheduleEditorView.swift:88-92` (nowDate: nil) and `LightDetailView.swift:34-46` (`card.reportedDuty` in scope). The 44 pt `MiniDayCurve` (`MiniDayCurve.swift:39-49`) already draws the actual-duty dot and speaks both values (54-60).
- **SF3:** hold label composed `LightingView.swift:339-363`; `returnsToText` 496-500 is the pure-helper model; the schedule caption under the mini curve at 320-329 is where a convergence line belongs. `ScheduleCurve.duty(at:)` exists (`ScheduleCurve.swift:53-55`). No deadband constant reaches the client — pick 0.01 (1 %) as the client-side "meaningfully different" threshold, in `Dimming`.
- Kit test idioms: `DimmingTests`, `AuditPhraseTests`, `AuditRowTests`, `ScheduleLibraryTests` (stub via `StubTransport` keyed by operation id — copy `ScheduleClientTests.swift:9-25`).

---

### Task 1: kit — `Dimming.percent` + convergence caption + floor copy

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/Dimming.swift`
- Test: `BellasReefKit/Tests/BellasReefKitTests/DimmingTests.swift`

**Interfaces:**
- Produces:
  - `Dimming.percent(_ duty: Double) -> Int` — `Int((duty * 100).rounded())`.
  - `Dimming.convergenceThreshold: Double = 0.01`.
  - `Dimming.convergenceCaption(reportedDuty: Double?, targetDuty: Double?) -> String?` — nil unless both present and `abs(reported - target) > convergenceThreshold`; else `"Catching up to the schedule — now \(percent(reported))%, heading to \(percent(target))%"`.
  - `Dimming.floorFootnote: String = "Below \(percent(minUsableDuty))% this dimmer is off — points under \(percent(minUsableDuty))% run at 0%."` (compose from the constant, no literal 8s).

- [ ] **Step 1: Failing tests** (extend `DimmingTests`):

```swift
@Test("percent rounds instead of truncating — 0.29 is 29, not 28")
func percentRounds() {
    #expect(Dimming.percent(0.29) == 29)
    #expect(Dimming.percent(0.005) == 1)
    #expect(Dimming.percent(0.0) == 0)
    #expect(Dimming.percent(1.0) == 100)
}

@Test("convergence caption appears only while meaningfully apart")
func convergenceCaption() {
    #expect(Dimming.convergenceCaption(reportedDuty: 0.45, targetDuty: 0.79) ==
            "Catching up to the schedule — now 45%, heading to 79%")
    #expect(Dimming.convergenceCaption(reportedDuty: 0.79, targetDuty: 0.792) == nil)
    #expect(Dimming.convergenceCaption(reportedDuty: nil, targetDuty: 0.5) == nil)
    #expect(Dimming.convergenceCaption(reportedDuty: 0.5, targetDuty: nil) == nil)
}
```

- [ ] **Step 2:** Run kit tests — expect the new ones FAIL (missing symbols).
- [ ] **Step 3:** Implement in `Dimming.swift`, matching its doc-comment style (each member carries a one-line "why").
- [ ] **Step 4:** Kit tests PASS.
- [ ] **Step 5:** Commit: `feat(kit): rounded percent, convergence caption, floor footnote in Dimming`

---

### Task 2: kit — audit rendering (SF7)

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/AuditPhrase.swift`, `AuditRow.swift`
- Test: `AuditPhraseTests.swift`, `AuditRowTests.swift`

**Interfaces:**
- Produces:
  - `AuditPhrase.title(action:deviceName:reason:)` — same signature (callers pass the schedule's `name` through `deviceName` for `schedule.created/updated/deleted`, the channel's display name for assign/unassign) — new cases:
    - `"schedule.created"` → `"Created schedule \(name ?? "")"` (trim to `"Created a schedule"` when nil)
    - `"schedule.updated"` → `"Edited schedule \(name)"` / `"Edited a schedule"`
    - `"schedule.deleted"` → `"Deleted schedule \(name)"` / `"Deleted a schedule"`
    - `"schedule.assigned"` → `"Schedule assigned\(name.map { " to \($0)" } ?? "")"`
    - `"schedule.unassigned"` → `"Schedule unassigned\(name.map { " from \($0)" } ?? "")"`
  - `AuditRow.subjectId(deviceId:payload:)` — checks, in order: `deviceId` param, `payload["device_id"]`, `payload["target"]`, `payload["channel_id"]`, then `payload["name"]` for the three schedule-CRUD verbs? **No** — keep `subjectId` id-only; instead add `AuditRow.subjectName(payload:) -> String?` returning `payload["name"] as? String`, and the view prefers `subjectName` when the id resolves to nothing. (Keeps ids and labels distinct; the catalog lookup stays id-keyed.)

- [ ] **Step 1: Failing tests:**

```swift
@Test("schedule verbs render as sentences, not raw tokens")
func scheduleVerbs() {
    #expect(AuditPhrase.title(action: "schedule.created", deviceName: "Reef Day") == "Created schedule Reef Day")
    #expect(AuditPhrase.title(action: "schedule.assigned", deviceName: "Meter Check") == "Schedule assigned to Meter Check")
    #expect(AuditPhrase.title(action: "schedule.unassigned", deviceName: nil) == "Schedule unassigned")
}

@Test("subjectId resolves device_id and channel_id payload keys")
func subjectKeys() {
    #expect(AuditRow.subjectId(deviceId: nil, payload: ["device_id": "ds18b20-a"]) == "ds18b20-a")
    #expect(AuditRow.subjectId(deviceId: nil, payload: ["channel_id": "pca9685-0"]) == "pca9685-0")
    #expect(AuditRow.subjectId(deviceId: "row-wins", payload: ["device_id": "x"]) == "row-wins")
}
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Update `AuditLogView.swift:106-112` to feed schedule rows: `deviceName: subject.map { model.catalog?.name(for: $0) ?? $0 } ?? AuditRow.subjectName(payload: payload)`. Build the app (`xcodegen generate && xcodebuild build …`).
- [ ] **Step 6:** Commit: `feat(kit): audit phrases for schedule.* events; subject id resolves device_id/channel_id`

---

### Task 3: kit — ghost-channel arithmetic (MF1's engine)

**Files:**
- Create: `BellasReefKit/Sources/BellasReefKit/ScheduleGhosts.swift`
- Test: `BellasReefKit/Tests/BellasReefKitTests/ScheduleGhostsTests.swift`

**Interfaces:**
- Produces:

```swift
/// Channels a schedule is assigned to that no adopted light currently claims.
/// Assignment survives unadopt/forget on the hub by design (spec 2026-08-19);
/// these are the ids every adopted-filtered surface goes blind to — the 2026-08-23
/// UX review's MF1. Pure set arithmetic so it can carry a test.
public enum ScheduleGhosts {
    public static func channels(
        assigned: [String],
        devices: [Components.Schemas.DeviceView]
    ) -> [String] {
        let adopted = Set(devices.filter { $0.adopted == true }.map(\.deviceId))
        return assigned.filter { !adopted.contains($0) }.sorted()
    }
}
```

- [ ] **Step 1: Failing tests** (new suite, header + `@Suite("ScheduleGhosts")` per convention): assigned `["pca9685-0","pi-pwm-0"]` with only `pi-pwm-0` adopted → `["pca9685-0"]`; empty assigned → `[]`; unadopted device row present in `devices` still counts as ghost.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit: `feat(kit): ScheduleGhosts — the assigned-minus-adopted set every surface needs`

---

### Task 4: MF1 in the views — ghosts visible, unassignable, and warned about

**Files:**
- Modify: `BellasReef/Views/ScheduleEditorView.swift` (`assignSection` 232-268)
- Modify: `BellasReef/Views/SchedulesView.swift` (rows 112-124, delete dialog 94-109, `delete` 126-139)
- Modify: `BellasReef/Views/AdoptDeviceSheet.swift` (hoist proposed id; confirm message 129-145)
- Modify: `BellasReef/Views/SystemView.swift` (clear dialog copy 255-258)

**Interfaces:**
- Consumes: `ScheduleGhosts.channels`, `ScheduleLibrary.unassign(channelId:)` (works on ghosts), `ScheduleLibrary.schedule(assignedTo:)`, `DeviceCatalog.name(for:)`.

- [ ] **Step 1: Editor — a "Still assigned" subsection.** In `assignSection`, after the adopted `ForEach`, compute `let ghosts = ScheduleGhosts.channels(assigned: schedule.assignedChannels, devices: model.catalog?.devices ?? [])` and render:

```swift
            if !ghosts.isEmpty {
                Section {
                    ForEach(ghosts, id: \.self) { channelId in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(model.catalog?.name(for: channelId) ?? channelId)
                                    .font(Theme.body)
                                Text("Not adopted — output resumes if this channel is adopted again.")
                                    .font(Theme.caption)
                                    .foregroundStyle(Theme.secondaryText)
                            }
                            Spacer()
                            Button("Unassign") {
                                Task { await library.unassign(channelId: channelId) }
                            }
                            .font(Theme.caption)
                        }
                    }
                } header: {
                    Text("Still assigned")
                }
            }
```

(Adjust `Theme` font tokens to the ones the surrounding rows actually use; place inside the existing "Assigned lights" `Section` or as a sibling — match how the file structures sections. The `library` reference follows however `toggle` reaches it at 272-294.)

- [ ] **Step 2: SchedulesView — the row and the delete dialog say the truth.** Row subtitle: when the schedule has ghosts, append `" · \(ghostCount) not adopted"`. Delete dialog: keep the plain delete for unassigned schedules; for assigned ones add a second destructive button:

```swift
            Button("Unassign from \(schedule.assignedChannels.count) light(s) and delete", role: .destructive) {
                Task { await unassignAllAndDelete(schedule, library: library) }
            }
```

with:

```swift
    private func unassignAllAndDelete(_ schedule: Components.Schemas.ScheduleView,
                                      library: ScheduleLibrary) async {
        for channelId in schedule.assignedChannels {
            await library.unassign(channelId: channelId)
        }
        await delete(schedule, library: library)
    }
```

(`ScheduleLibrary.unassign` already refreshes and records failures — check its error surface and stop the loop on a thrown/recorded failure so `delete` doesn't fire after a failed unassign; mirror how `delete` reports via `problem`.)

- [ ] **Step 3: Adopt sheet — warn before resuming a ghost's curve.** Hoist the id (168-171) to:

```swift
    private var proposedDeviceId: String {
        "\(driverType.rawValue)-\(capability.channel)"
            .lowercased().replacingOccurrences(of: " ", with: "-")
    }
```

(use it inside `adopt()` too), and extend the confirm message (129-145):

```swift
            } message: {
                if let ghost = model.library?.schedule(assignedTo: proposedDeviceId) {
                    Text("Adopting starts real output on this channel as soon as the "
                         + "engine's schedule runs. “\(ghost.name)” is still assigned to "
                         + "this channel and resumes immediately. Only adopt hardware "
                         + "you have bench-verified.")
                } else {
                    Text("Adopting starts real output on this channel as soon as the "
                         + "engine's schedule runs. Only adopt hardware you have "
                         + "bench-verified.")
                }
            }
```

(Verify `AppModel` exposes `library` as `model.library` — the exploration says `SchedulesView`/editor reach a `ScheduleLibrary`; follow the same access path. If the sheet can't reach it cheaply, thread it the way `model.catalog` is threaded.)

- [ ] **Step 4: SystemView clear-dialog copy** (255-258) — replace the last sentence:

```swift
            Text("Its name and settings are deleted for good. Readings it "
                 + "already recorded stay in history. A schedule assigned to "
                 + "this channel stays assigned — unassign it under Lighting "
                 + "if the hardware is gone for good.")
```

- [ ] **Step 5:** `xcodegen generate && xcodebuild build …` — green. Kit tests still green.
- [ ] **Step 6:** Commit: `fix(schedules): ghost assignments are visible, unassignable, and warned about at adopt (MF1)`

---

### Task 5: SF1 + MF2 + MF3 sweep

**Files:**
- Modify: `LightingView.swift` (343, 511, 529, 532 → `Dimming.percent`; also 371/386 for consistency), `TankView.swift` (676, 696, 716, 718), `ScheduleEditorView.swift` (110-113 a11y), `AuditLogView.swift` (172), `AdoptDeviceSheet.swift` (203), `BellasReefKit/Sources/BellasReefKit/DeviceCatalog.swift` (80)

- [ ] **Step 1:** Replace all seven truncating sites with `Dimming.percent(...)` — e.g. `Text("\(Dimming.percent(duty))%")`, `"held at \(Dimming.percent(hold.duty)) percent"`. Leave the already-correct sites alone (or switch them to the helper where the diff stays one-line — reviewer's call; do not restructure).
- [ ] **Step 2:** MF2 — on the duty TextField:

```swift
                        TextField("%", text: $point.dutyPercentText)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 64)
                            .accessibilityLabel("Brightness percent")
                            .accessibilityIdentifier("schedule-point-duty")
```

- [ ] **Step 3:** MF3 — three sites: `HumanError.describe(error)` instead of `"\(error)"` (`DeviceCatalog` is kit-side; check `HumanError` is importable there — same module, so it's direct).
- [ ] **Step 4:** For `DeviceCatalog.swift:80`, extend/adjust any kit test asserting the failed-state string (grep `DeviceCatalogTests` for `.failed`).
- [ ] **Step 5:** Kit tests + app build green.
- [ ] **Step 6:** Commit: `fix(ui): rounded percents everywhere, duty field a11y label, human-readable errors (SF1, MF2, MF3)`

---

### Task 6: SF9 — the draft follows the hub until touched

**Files:**
- Modify: `BellasReef/Views/LightingView.swift` (176-182, 379-381, add `.onChange`)

- [ ] **Step 1:** Add `@State private var draftTouched = false` beside `proposedDuty`. In the Slider's `onEditingChanged` (379-381): `if editing { draftTouched = true }` before the existing snap logic. Add after `.onDisappear` (443-445):

```swift
        .onChange(of: card.reportedDuty) {
            // The seed is a convenience, not a proposal: until the operator
            // touches the slider, it tracks the hub so the caption cannot nag
            // about a choice nobody made (UX review 2026-08-23, SF9 — the
            // schedule moved and the app said "Set to 82% · not applied yet").
            if !draftTouched && !submitting {
                proposedDuty = (card.reportedDuty ?? 0) * 100
            }
        }
```

Also reset `draftTouched = false` in the same `.onDisappear` branch that re-seeds, and after a successful hold submit (find where `submitting` completes — the hold command path at ~616 — and mirror wherever `proposedDuty` is reconciled there).

- [ ] **Step 2:** Update the `@State` doc comment (176-181) to describe the touched-gate (it currently documents the never-resync behavior as intentional — it no longer is, pre-touch).
- [ ] **Step 3:** App build green. Manual sanity is deferred to the walkthrough note in the PR (sim check optional).
- [ ] **Step 4:** Commit: `fix(lighting): slider draft tracks the hub until first touch (SF9)`

---

### Task 7: SF2 + SF4 — the editor and charts teach the floor and show the truth

**Files:**
- Modify: `BellasReef/Views/ScheduleChart.swift` (floor band + reported-duty mark + new parameter)
- Modify: `BellasReef/Views/ScheduleEditorView.swift` (Points footer 100-127; call site 88-92)
- Modify: `BellasReef/Views/LightDetailView.swift` (call site 34-46)
- Modify: `BellasReef/Views/LightingView.swift` (320-329 — SF3 convergence caption)

- [ ] **Step 1: ScheduleChart** — add `var reportedDuty: Double? = nil`; inside the `Chart`:

```swift
            RectangleMark(
                xStart: .value("Hour", 0), xEnd: .value("Hour", 24),
                yStart: .value("Brightness", 0),
                yEnd: .value("Brightness", Dimming.minUsableDuty * 100)
            )
            .foregroundStyle(Theme.tertiaryText.opacity(0.12))
```

(first, so the curve draws over it), and beside the now-RuleMark:

```swift
            if let nowDate, let reportedDuty {
                PointMark(
                    x: .value("Now", Double(curve.secondsOfDay(for: nowDate)) / 3600),
                    y: .value("Actual", reportedDuty * 100)
                )
                .foregroundStyle(Theme.attention)
                .symbolSize(60)
            }
```

- [ ] **Step 2:** Call sites: editor stays `ScheduleChart(curve: curve, nowDate: nil)`; light detail becomes `ScheduleChart(curve: schedule.curve, nowDate: context.date, reportedDuty: card.reportedDuty)`.
- [ ] **Step 3: Editor footer** — Points `Section` gains `footer: Text(Dimming.floorFootnote)` (idiom from 132-137). Do NOT tighten validation: sub-8 % points are legal on the wire and mean "off" — the footer says so; blocking them would forbid curves that deliberately floor at 0 via low points.
- [ ] **Step 4: SF3** — under the mini curve (`LightingView.swift` 320-329), inside the existing `TimelineView`-supplied date context (check whether one wraps this block; the hold row at 339 has one — reuse the nearest date source or the schedule caption's own), add:

```swift
                if let schedule = card.schedule,
                   let caption = Dimming.convergenceCaption(
                       reportedDuty: card.reportedDuty,
                       targetDuty: schedule.curve.duty(at: Date())
                   ) {
                    Text(caption)
                        .font(Theme.caption)
                        .foregroundStyle(Theme.secondaryText)
                }
```

(If the surrounding block lacks a timeline context, `Date()` re-evaluates on state-frame updates — which arrive on every engine publish, i.e. every ~2 min mid-slew — acceptable; note it in the code only if a reviewer asks.)

- [ ] **Step 5:** Kit tests + app build green.
- [ ] **Step 6:** Commit: `feat(schedules): charts shade the sub-8% floor, show actual duty, and the card says when it's catching up (SF2–SF4)`

---

### Task 8: PR, CI, merge

- [ ] **Step 1:** Full local gate: kit tests + `xcodegen generate` + app build, both green.
- [ ] **Step 2:** Push branch, PR `fix(ux): 2026-08-23 review — must-fixes and schedule-soon batch (MF1–MF3, SF1–SF4, SF7, SF9)`. PR body: link the review draft path, list findings→commits, note the two behavior decisions (sub-8 % points stay legal + footer; ghost unassign uses the existing endpoint). CI green → merge. Update the review draft's tier list with "shipped 2026-08-23" annotations? **No** — leave the draft untouched; the session report records what shipped.
- [ ] **Step 3:** Delete from the `ios-schedules-deferred-minors` memory any entries this plan landed (the a11y label). Done in the main session, not by a subagent.

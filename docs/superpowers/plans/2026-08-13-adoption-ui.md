# Adoption UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The app half of the capability registry — a Hardware section on the System page listing adopted devices and unclaimed channels, with adopt (safety-confirmed for actuators) and unadopt, per `docs/superpowers/specs/2026-08-13-adoption-ui-design.md`.

**Architecture:** Three kit wrappers on `HubClient` in the existing typed-outcome idiom (`capabilities()`, `bind(_:) -> BindOutcome`, `unbind(deviceId:) -> UnbindOutcome`); a `hardware` section on `SystemView` following its `pairedDevices` pattern, loading through the existing `loadEverything()`/`.refreshable` path; an `AdoptDeviceSheet` view file for the adopt flow. App-only — no backend or contract change.

**Tech Stack:** Swift 6.2 strict concurrency, SwiftUI, swift-openapi-generator client (ops already generated at spec 3.5), Swift Testing for kit tests, XCUITest for the screen test.

## Global Constraints

- Repo: `/Users/david/visualstudio/bellasreef-ios`. Work on `main`; push only in Task 3's final step.
- Kit tests: `cd BellasReefKit && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild test -scheme BellasReefKit-Package -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -skipPackagePluginValidation -skipMacroValidation`. App build: same env/flags with `-project BellasReef.xcodeproj -scheme BellasReef` and `build`. SourceKit "No such module" diagnostics are indexing noise; xcodebuild is the arbiter.
- Test doubles `StubTransport`, `MemoryCredentials`, `CallLog` live in `BellasReefKit/Tests/BellasReefKitTests/PairingTests.swift` (internal to the test target — reuse, never redefine). New test files carry their own file-private `anyHub`/`json` helpers.
- Generated enum case names in switches (`.notFound`, `.conflict`, `.noContent`, `.unprocessableContent`) must match what the generator actually produced — if the compiler disagrees with this plan's spelling, follow the compiler and note the deviation in your report.
- The safety confirm appears for actuator sources only (`pi-pwm`, `pca9685`), never for `w1-bus`. Exact confirm copy (verbatim): "Adopting starts real output on this channel as soon as the engine's schedule runs. Only adopt hardware you have bench-verified."
- Nothing in any task adopts a real channel on the live hub. The live smoke in Task 4 is read-only.
- Conventional commits with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.

## Interfaces already in the codebase

- `HubClient` (actor, `BellasReefKit/Sources/BellasReefKit/HubClient.swift`): wrapper idiom is a switch over the generated output with `case .unauthorized: throw credentialWasRejected()` and `.undocumented` → `ClientError.unexpected` (see `devices()` at ~line 186). Add new code after the `devices()` wrapper under a `// MARK: Capabilities and adoption`.
- Generated ops (in `BellasReefAPI`, from openapi.json): `listCapabilities` GET `/api/v1/capabilities` (200/401/422), `bindDevice` POST `/api/v1/devices` (200/401/404/409/422), `unbindDevice` DELETE `/api/v1/devices/{device_id}` (204/401/404/422). Schemas: `Components.Schemas.CapabilityView` (`source`, `channel`, `detail`, `announcedAt`, `boundTo`), `Components.Schemas.BindDeviceRequest` (`deviceId`, `driverType`, `channel`, `role`, `displayName`, `location`, `pollIntervalS`), `Components.Schemas.BoundDevice` (`deviceId`, `created`, `driverType`, `channel`). Generated Swift uses camelCase; JSON wire format is snake_case.
- `SystemView` (`BellasReef/Views/SystemView.swift`): `@State` per concern + `loadEverything()` (async-let fan-out) + `.refreshable`; sections are `@ViewBuilder` computed properties (`pairedDevices` is the model to copy); inline failure rows in `Theme.attention`/`Theme.tertiaryText`; 44pt row minimums; destructive actions behind `confirmationDialog`.
- `model.client` is `HubClient?` on `AppModel`; views call it directly (see `revoke(_:)` in SystemView).

---

### Task 1: Kit wrappers and their tests

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/HubClient.swift` (after `devices()`, ~line 196)
- Test: `BellasReefKit/Tests/BellasReefKitTests/AdoptionTests.swift` (create)

**Interfaces:**
- Consumes: generated ops/schemas listed above; `credentialWasRejected()`.
- Produces (Task 2 renders these): `capabilities() async throws -> [Components.Schemas.CapabilityView]`; `enum BindOutcome: Sendable, Equatable { case bound(deviceId: String, created: Bool); case channelGone; case alreadyBound; case roleNotLegal }`; `bind(_ request: Components.Schemas.BindDeviceRequest) async throws -> BindOutcome`; `enum UnbindOutcome: Sendable, Equatable { case unbound, alreadyUnbound }`; `unbind(deviceId: String) async throws -> UnbindOutcome`.

- [ ] **Step 1: Write the failing tests**

```swift
// Bella's Reef iOS — closed source.

import Foundation
import Testing

@testable import BellasReefKit

private let anyHub = Hub(
    name: "Bella's Reef", baseURL: URL(string: "http://hub.invalid:8000")!, discovered: false
)

private func json(_ text: String) -> Data { Data(text.utf8) }

/// Stub bodies for the adoption endpoints. Wire format is snake_case.
private let oneFreeChannel = #"""
[{"source": "pca9685", "channel": "0",
  "detail": {"i2c_address": "0x40"},
  "announced_at": "2026-08-13T00:00:00Z", "bound_to": null}]
"""#

private func stub(_ handler: @escaping @Sendable (String) async throws -> (Int, Data?)) -> HubClient {
    HubClient(
        hub: anyHub, tokens: MemoryCredentials(token: "refresh"),
        transport: StubTransport { operation, _, _ in
            if operation == "mintToken" {
                return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
            }
            return try await handler(operation)
        }
    )
}

@Suite("Adoption wrappers")
struct AdoptionTests {

    @Test("capabilities decode, including a null bound_to")
    func capabilitiesDecode() async throws {
        let client = stub { _ in (200, json(oneFreeChannel)) }
        let rows = try await client.capabilities()
        #expect(rows.count == 1)
        #expect(rows[0].source.rawValue == "pca9685")
        #expect(rows[0].channel == "0")
        #expect(rows[0].boundTo == nil)
    }

    @Test("a successful bind reports the hub's id and whether it created")
    func bindSucceeds() async throws {
        let client = stub { operation in
            #expect(operation == "bindDevice")
            return (200, json(#"""
                {"device_id": "led-blue", "created": false,
                 "driver_type": "pca9685", "channel": "0"}
                """#))
        }
        let outcome = try await client.bind(
            .init(deviceId: "pca9685-0", driverType: .pca9685, channel: "0",
                  role: .light, displayName: "Blue light")
        )
        // created: false is match-before-create: the channel already carried
        // a device, the hub adopted it in place and its id wins over ours.
        #expect(outcome == .bound(deviceId: "led-blue", created: false))
    }

    @Test("each documented refusal is its own outcome")
    func bindRefusals() async throws {
        for (status, expected) in [(404, HubClient.BindOutcome.channelGone),
                                   (409, .alreadyBound),
                                   (422, .roleNotLegal)] {
            let client = stub { _ in (status, json(#"{"detail": "refused"}"#)) }
            let outcome = try await client.bind(
                .init(deviceId: "pca9685-0", driverType: .pca9685, channel: "0",
                      role: .light, displayName: "Blue light")
            )
            #expect(outcome == expected, "status \(status)")
        }
    }

    @Test("unbind distinguishes done from already-done")
    func unbindOutcomes() async throws {
        let gone = stub { _ in (204, nil) }
        #expect(try await gone.unbind(deviceId: "led-blue") == .unbound)
        let already = stub { _ in (404, json(#"{"detail": "no such device"}"#)) }
        #expect(try await already.unbind(deviceId: "led-blue") == .alreadyUnbound)
    }
}
```

- [ ] **Step 2: Run to verify failure**

Run the Global Constraints kit-test command plus `-only-testing:BellasReefKitTests/AdoptionTests`.
Expected: FAIL — compile errors, `capabilities`/`bind`/`unbind`/`BindOutcome` do not exist.

- [ ] **Step 3: Implement the wrappers**

In `HubClient.swift` after `devices()`:

```swift
    // MARK: Capabilities and adoption

    /// What the hardware can offer, and what has been claimed. Tier one of
    /// the registry: nothing here is a device until an operator binds it.
    public func capabilities() async throws -> [Components.Schemas.CapabilityView] {
        switch try await client.listCapabilities() {
        case let .ok(response): return try response.body.json
        case .unauthorized: throw credentialWasRejected()
        case .unprocessableContent:
            throw ClientError.unexpected("the hub rejected the capabilities query")
        case let .undocumented(statusCode, _):
            throw ClientError.unexpected("capabilities returned \(statusCode)")
        }
    }

    /// Every documented ending of `POST /api/v1/devices`. Distinct cases
    /// because each needs different words and a different way out — the 409
    /// in particular means the list on screen is stale, not that the operator
    /// did anything wrong.
    public enum BindOutcome: Sendable, Equatable {
        /// 200. `created: false` is match-before-create: the channel already
        /// carried a device, which was adopted in place under its own id.
        case bound(deviceId: String, created: Bool)
        /// 404 — the channel is no longer announced.
        case channelGone
        /// 409 — another device claimed the channel since the list loaded.
        case alreadyBound
        /// 422 — the role is not legal for this device.
        case roleNotLegal
    }

    public func bind(
        _ request: Components.Schemas.BindDeviceRequest
    ) async throws -> BindOutcome {
        switch try await client.bindDevice(body: .json(request)) {
        case let .ok(response):
            let bound = try response.body.json
            return .bound(deviceId: bound.deviceId, created: bound.created)
        case .notFound: return .channelGone
        case .conflict: return .alreadyBound
        case .unprocessableContent: return .roleNotLegal
        case .unauthorized: throw credentialWasRejected()
        case let .undocumented(statusCode, _):
            throw ClientError.unexpected("bind returned \(statusCode)")
        }
    }

    /// Every documented ending of `DELETE /api/v1/devices/{device_id}`.
    public enum UnbindOutcome: Sendable, Equatable {
        case unbound
        /// 404 — unknown, or already unbound. Either way the channel is free.
        case alreadyUnbound
    }

    public func unbind(deviceId: String) async throws -> UnbindOutcome {
        switch try await client.unbindDevice(path: .init(deviceId: deviceId)) {
        case .noContent: return .unbound
        case .notFound: return .alreadyUnbound
        case .unauthorized: throw credentialWasRejected()
        case .unprocessableContent:
            throw ClientError.unexpected("the hub rejected the device id")
        case let .undocumented(statusCode, _):
            throw ClientError.unexpected("unbind returned \(statusCode)")
        }
    }
```

If the generator's case names differ (e.g. no `.conflict` case exists), match the generated code and record the deviation.

- [ ] **Step 4: Run the full kit suite** — the Global Constraints command, no filter. Expected: all pass (43 existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add BellasReefKit/Sources/BellasReefKit/HubClient.swift \
        BellasReefKit/Tests/BellasReefKitTests/AdoptionTests.swift
git commit -m "feat(adoption): kit wrappers for capabilities, bind, unbind

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: The Hardware section and the adopt sheet

**Files:**
- Create: `BellasReef/Views/AdoptDeviceSheet.swift`
- Modify: `BellasReef/Views/SystemView.swift` (new state + `hardware` section + load wiring)

**Interfaces:**
- Consumes: Task 1's `capabilities()`, `bind(_:)`, `unbind(deviceId:)`; existing `model.client`, `model.clients()` patterns, `Theme`, `HubMemory` conventions in SystemView.
- Produces: accessibility identifiers Task 3's UI test depends on, exactly: `"hardware-available-channel"` (each available-channel row button), `"adopt-name-field"`, `"adopt-confirm-button"`, `"unadopt-<device_id>"` (each unadopt button).

- [ ] **Step 1: Add state and loading to SystemView**

Add alongside the existing `@State` block:

```swift
    @State private var capabilities: [Components.Schemas.CapabilityView]?
    @State private var hardwareDevices: [Components.Schemas.DeviceView]?
    @State private var hardwareFailed = false
    @State private var adopting: Components.Schemas.CapabilityView?
    @State private var unadopting: Components.Schemas.DeviceView?
    @State private var unadoptProblem: String?
```

Extend `loadEverything()`'s fan-out with a third async-let (same shape as `devices`):

```swift
        async let hardware: Void = loadHardware()
```
(and `await hardware` beside the existing `await devices`.)

Add the loader:

```swift
    private func loadHardware() async {
        guard let client = model.client else { return }
        do {
            async let caps = client.capabilities()
            async let devs = client.devices()
            capabilities = try await caps
            hardwareDevices = try await devs
            hardwareFailed = false
        } catch {
            hardwareFailed = true
        }
    }
```

- [ ] **Step 2: Add the `hardware` section**

A `@ViewBuilder` computed property, placed in the `List` directly below `pairedDevices`, mirroring its loading/failed/stale states:

```swift
    /// Inventory and lifecycle only — controls live on the function tabs
    /// (design ruling 2026-08-13: System is never a junk drawer).
    @ViewBuilder
    private var hardware: some View {
        Section {
            if let hardwareDevices, let capabilities {
                ForEach(hardwareDevices, id: \.deviceId) { device in
                    adoptedRow(device)
                }
                let free = capabilities.filter { $0.boundTo == nil }
                if free.isEmpty && hardwareDevices.isEmpty {
                    Text("The hub has not announced any hardware.")
                        .font(Theme.caption)
                        .foregroundStyle(Theme.tertiaryText)
                }
                if !free.isEmpty {
                    Text("Available channels")
                        .font(Theme.caption)
                        .foregroundStyle(Theme.tertiaryText)
                    ForEach(free, id: \.channel) { capability in
                        Button { adopting = capability } label: {
                            availableRow(capability)
                        }
                        .accessibilityIdentifier("hardware-available-channel")
                    }
                }
                if hardwareFailed {
                    Text("Could not refresh this list — it may be out of date.")
                        .font(Theme.caption)
                        .foregroundStyle(Theme.attention)
                }
            } else if hardwareFailed {
                Text("Could not ask the hub what hardware it has.")
                    .font(Theme.caption)
                    .foregroundStyle(Theme.tertiaryText)
            } else {
                ProgressView().controlSize(.small)
            }

            if let unadoptProblem {
                Label(unadoptProblem, systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.caption)
                    .foregroundStyle(Theme.attention)
            }
        } header: {
            Text("Hardware")
        } footer: {
            Text("Adopting a channel makes it a device the engine may command. "
                 + "Controls live on the tab that uses the device; this list is "
                 + "the inventory.")
        }
    }

    @ViewBuilder
    private func adoptedRow(_ device: Components.Schemas.DeviceView) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(device.displayName ?? device.deviceId)
                    .foregroundStyle(Theme.primaryText)
                Text(deviceSubtitle(device))
                    .font(Theme.caption)
                    .foregroundStyle(Theme.tertiaryText)
            }
            Spacer()
            Button("Unadopt", role: .destructive) { unadopting = device }
                .buttonStyle(.borderless)
                .accessibilityIdentifier("unadopt-\(device.deviceId)")
        }
        .frame(minHeight: 44)
    }

    private func deviceSubtitle(_ device: Components.Schemas.DeviceView) -> String {
        var parts = [device.driverId]
        if let role = device.role { parts.append(role) }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func availableRow(_ capability: Components.Schemas.CapabilityView) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(capability.source.rawValue) · channel \(capability.channel)")
                    .foregroundStyle(Theme.primaryText)
                Text("announced, not adopted")
                    .font(Theme.caption)
                    .foregroundStyle(Theme.tertiaryText)
            }
            Spacer()
            Image(systemName: "plus.circle")
                .foregroundStyle(Theme.accent)
        }
        .frame(minHeight: 44)
    }
```

(If `DeviceView`'s generated property for the driver is named differently — check the generated schema, it follows `driver_id` → `driverId` — match the generated name.)

- [ ] **Step 3: Wire the sheet and the unadopt dialog**

After the existing `.sheet(isPresented: $addingDevice)`:

```swift
            .sheet(item: $adopting) { capability in
                AdoptDeviceSheet(capability: capability) {
                    Task { await loadHardware() }
                }
            }
            .confirmationDialog(
                "Unadopt this device?",
                isPresented: Binding(
                    get: { unadopting != nil },
                    set: { if !$0 { unadopting = nil } }
                ),
                titleVisibility: .visible,
                presenting: unadopting
            ) { device in
                Button("Unadopt \(device.displayName ?? device.deviceId)",
                       role: .destructive) {
                    Task { await unadopt(device) }
                }
                Button("Cancel", role: .cancel) {}
            } message: { _ in
                Text("The engine stops commanding this channel and it returns to "
                     + "its safe state. History is kept — adopting the same "
                     + "hardware again reattaches it.")
            }
```

`Components.Schemas.CapabilityView` needs `Identifiable` for `.sheet(item:)` — add a small conformance in the app target (file-private extension in SystemView.swift):

```swift
extension Components.Schemas.CapabilityView: @retroactive Identifiable {
    public var id: String { "\(source.rawValue):\(channel)" }
}
```

And the unadopt action:

```swift
    private func unadopt(_ device: Components.Schemas.DeviceView) async {
        unadoptProblem = nil
        do {
            _ = try await model.client?.unbind(deviceId: device.deviceId)
        } catch {
            unadoptProblem = "\(error)"
        }
        await loadHardware()
    }
```

(Both `UnbindOutcome` cases end with the channel free, so the outcome value is deliberately unused; a thrown error is the only failure worth words.)

- [ ] **Step 4: Write AdoptDeviceSheet.swift**

```swift
// Bella's Reef iOS — closed source.

import BellasReefAPI
import BellasReefKit
import SwiftUI

/// Adopt one announced channel as a device. The channel and driver are facts
/// from the capability row and are shown, never typed. The safety confirm is
/// the guardrail that lets these screens exist while actuator bring-up is
/// still bench-gated: the consequence is stated at the moment of decision.
struct AdoptDeviceSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    let capability: Components.Schemas.CapabilityView
    let onAdopted: () -> Void

    @State private var name: String
    @State private var confirming = false
    @State private var working = false
    @State private var problem: String?

    init(capability: Components.Schemas.CapabilityView, onAdopted: @escaping () -> Void) {
        self.capability = capability
        self.onAdopted = onAdopted
        _name = State(initialValue: Self.seedName(for: capability))
    }

    /// Actuator sources get the safety confirm; a probe read has no failure
    /// mode worth the friction.
    private var isActuator: Bool { capability.source.rawValue != "w1-bus" }

    var body: some View {
        NavigationStack {
            Form {
                Section("Channel") {
                    LabeledContent("Source", value: capability.source.rawValue)
                    LabeledContent("Channel", value: capability.channel)
                    LabeledContent("Driver", value: driverType.rawValue)
                }
                Section("Device") {
                    TextField("Name", text: $name)
                        .accessibilityIdentifier("adopt-name-field")
                    // One legal role today. A picker rather than a label so
                    // future roles have a home; disabled because a choice of
                    // one is not a choice.
                    if isActuator {
                        Picker("Role", selection: .constant("light")) {
                            Text("Light").tag("light")
                        }
                        .disabled(true)
                    }
                }
                Section {
                    Button {
                        if isActuator { confirming = true } else { Task { await adopt() } }
                    } label: {
                        if working { ProgressView() } else { Text("Adopt") }
                    }
                    .frame(minHeight: 44)
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || working)
                    .accessibilityIdentifier("adopt-confirm-button")

                    if let problem {
                        Label(problem, systemImage: "exclamationmark.triangle.fill")
                            .font(Theme.caption)
                            .foregroundStyle(Theme.attention)
                    }
                }
            }
            .navigationTitle("Adopt hardware")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .confirmationDialog(
                "Start real output?",
                isPresented: $confirming,
                titleVisibility: .visible
            ) {
                Button("Adopt", role: .destructive) { Task { await adopt() } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Adopting starts real output on this channel as soon as the "
                     + "engine's schedule runs. Only adopt hardware you have "
                     + "bench-verified.")
            }
        }
    }

    private var driverType: Components.Schemas.BindDeviceRequest.DriverTypePayload {
        switch capability.source.rawValue {
        case "w1-bus": .ds18b20
        case "pi-pwm": .piPwm
        default: .pca9685
        }
    }

    private static func seedName(for capability: Components.Schemas.CapabilityView) -> String {
        capability.source.rawValue == "w1-bus"
            ? "Temperature probe"
            : "Light \(capability.channel)"
    }

    private func adopt() async {
        working = true
        defer { working = false }
        problem = nil
        do {
            let proposed = "\(driverType.rawValue)-\(capability.channel)"
                .lowercased().replacingOccurrences(of: " ", with: "-")
            let outcome = try await model.client?.bind(
                .init(
                    deviceId: proposed,
                    driverType: driverType,
                    channel: capability.channel,
                    role: isActuator ? .light : nil,
                    displayName: name.trimmingCharacters(in: .whitespaces)
                )
            )
            switch outcome {
            case .bound:
                onAdopted()
                dismiss()
            case .channelGone:
                problem = "The hub no longer announces this channel. Pull to refresh the list."
            case .alreadyBound:
                problem = "Another device claimed this channel since the list loaded."
            case .roleNotLegal:
                problem = "The hub refused the role for this device."
            case nil:
                problem = "Not connected to the hub."
            }
        } catch {
            problem = "\(error)"
        }
    }
}
```

(Generated payload enum names — `DriverTypePayload`, `.piPwm`, `RolePayload.light` — follow swift-openapi-generator's conventions; if the compiler names them differently, match the generated code and record the deviation. If `role:` takes an optional enum the `nil` branch is already right.)

- [ ] **Step 5: Build the app** — Global Constraints app-build command. Expected: BUILD SUCCEEDED, no warnings introduced.

- [ ] **Step 6: Commit**

```bash
git add BellasReef/Views/SystemView.swift BellasReef/Views/AdoptDeviceSheet.swift
git commit -m "feat(adoption): the Hardware section — inventory, adopt, unadopt

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: The screen test, full suites, push

**Files:**
- Modify: `BellasReefUITests/PairingJourneyTests.swift` (append one test to the class)

**Interfaces:**
- Consumes: Task 2's accessibility identifiers verbatim: `"hardware-available-channel"`, `"adopt-name-field"`, `"adopt-confirm-button"`.

- [ ] **Step 1: Write the test** (append inside `PairingJourneyTests`, after `testTheApproverScreenIsReachable`)

```swift
    // MARK: - Hardware adoption

    /// System → Hardware, up to but not including adopting anything.
    ///
    /// Same philosophy as the approver test above: reaching the sheet and
    /// meeting the safety confirm is what this proves. Actually adopting
    /// starts real actuator output and stays a bench decision.
    func testTheAdoptSheetIsReachableAndGuarded() throws {
        let app = XCUIApplication()
        app.launch()

        let systemTab = app.tabBars.buttons["System"]
        guard systemTab.waitForExistence(timeout: 20) else {
            throw XCTSkip("not paired, so the System tab does not exist yet.")
        }
        systemTab.tap()

        XCTAssertTrue(
            app.staticTexts["Hardware"].waitForExistence(timeout: 10),
            "no Hardware section — the inventory has nowhere to live"
        )

        let channel = app.buttons["hardware-available-channel"].firstMatch
        guard channel.waitForExistence(timeout: 10) else {
            throw XCTSkip(
                "no unclaimed channel on this hub, so the adopt sheet cannot be "
                + "reached. Free a channel (unadopt) to exercise this test."
            )
        }
        channel.tap()

        let nameField = app.textFields["adopt-name-field"]
        XCTAssertTrue(nameField.waitForExistence(timeout: 10), "adopt sheet has no name field")

        // A blank name must not be submittable.
        nameField.tap()
        nameField.press(forDuration: 1.0)
        app.menuItems["Select All"].tap()
        nameField.typeText(XCUIKeyboardKey.delete.rawValue)
        XCTAssertFalse(
            app.buttons["adopt-confirm-button"].isEnabled,
            "an unnamed device was adoptable"
        )

        // Restore a name; the safety confirm must stand between the tap and
        // the bind for an actuator channel.
        nameField.typeText("Bench light")
        app.buttons["adopt-confirm-button"].tap()
        XCTAssertTrue(
            app.staticTexts["Start real output?"].waitForExistence(timeout: 5),
            "no safety confirm before adopting an actuator channel"
        )
        attach(app, named: "adopt-safety-confirm")

        app.buttons["Cancel"].firstMatch.tap()   // the dialog's cancel
        app.buttons["Cancel"].firstMatch.tap()   // the sheet's cancel
    }
```

- [ ] **Step 2: Run the kit suite** (Global Constraints kit command) — expected: all pass (47).

- [ ] **Step 3: Run the new UI test against the live hub** (bench test, from the repo root):

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
xcodebuild test -project BellasReef.xcodeproj -scheme BellasReef \
  -destination 'platform=iOS Simulator,id=6D480152-FCE6-42CA-A56E-BA6D25167560' \
  -skipPackagePluginValidation -skipMacroValidation \
  -only-testing:BellasReefUITests/PairingJourneyTests/testTheAdoptSheetIsReachableAndGuarded
```

The iPhone 17 Pro sim (that UDID) is paired to the live hub, which has unclaimed pca9685 channels — the test should PASS, not skip. A skip here is a finding: investigate before proceeding. The test adopts nothing (cancels out of the confirm).

- [ ] **Step 4: Commit and push**

```bash
git add BellasReefUITests/PairingJourneyTests.swift
git commit -m "test(adoption): the adopt sheet is reachable and guarded

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
```

---

### Task 4: Install, live smoke, closeout

- [ ] **Step 1: Build and install on the paired sim** (controller or agent):

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
cd /Users/david/visualstudio/bellasreef-ios
xcodebuild -project BellasReef.xcodeproj -scheme BellasReef \
  -destination 'platform=iOS Simulator,id=6D480152-FCE6-42CA-A56E-BA6D25167560' \
  -derivedDataPath /tmp/br-dd -skipPackagePluginValidation -skipMacroValidation build
xcrun simctl terminate 6D480152-FCE6-42CA-A56E-BA6D25167560 com.bellasreef.app 2>/dev/null
xcrun simctl install 6D480152-FCE6-42CA-A56E-BA6D25167560 \
  /tmp/br-dd/Build/Products/Debug-iphonesimulator/BellasReef.app
xcrun simctl launch 6D480152-FCE6-42CA-A56E-BA6D25167560 com.bellasreef.app
```

- [ ] **Step 2: Read-only live smoke.** Screenshot the System page (`xcrun simctl io <UDID> screenshot`): the Hardware section must show the adopted DS18B20 and the unclaimed pca9685 channels from the live hub. Adopt nothing.

- [ ] **Step 3: iOS CI green** — `gh run watch` the push from Task 3.

- [ ] **Step 4: Closeout.** David walks the screens; the led-blue adopt itself waits for bench Stage 1/2. Update the session memory (adoption UI shipped; next: bench stage 1, topology decision) and hand back.

## Self-Review

- Spec coverage: screens (Task 2), plumbing (Task 1), testing (Tasks 1/3), placement ruling encoded in the section footer and file comments; safety confirm exact copy in Task 2 Step 4 matches the spec verbatim; unadopt copy matches the spec's safe-direction wording. Out-of-scope items untouched.
- Placeholders: none; all code complete.
- Type consistency: `BindOutcome`/`UnbindOutcome` names match between Tasks 1 and 2; accessibility ids match between Tasks 2 and 3; generated-name caveats are flagged where the generator, not this plan, is the authority.
- Known risk: generated payload type spellings (`DriverTypePayload`, `.piPwm`) may differ — both affected tasks carry the follow-the-compiler instruction.

# C4 Identify-before-adopt, iOS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Identify step to the adopt sheet: adopt a PWM channel with no name, wait for hardware-io to rebuild it, pulse it at 50 % for 5 s through the ordinary override path, and let the operator confirm the right fixture lit before naming it.

**Architecture:** A `@MainActor @Observable` phase machine `IdentifyFlow` in BellasReefKit owns the sequence (bind, wait for a fresh state frame, pulse, answer, name or leave) and is tested against `StubTransport` and a canned frame source. `TankMonitor` gains one primitive, `nextFrame(for:newerThan:timeout:)`, behind a small `StateFrameSource` protocol so the flow never touches a socket in tests. `AdoptDeviceSheet` renders the phases and calls the flow's verbs; it keeps its safety confirm and its existing adopt-with-a-name path untouched.

**Tech Stack:** Swift 6.2, strict concurrency, SwiftUI, iOS 26, BellasReefKit SwiftPM package, Swift Testing, generated `BellasReefAPI` client (swift-openapi-generator).

**Spec:** `docs/superpowers/specs/2026-09-03-identify-before-adopt-design.md` in the backend repo (`/Users/david/visualstudio/bellasreef`). The plan argues from it; read "The flow", "The pulse", "Waiting for the rebuild", "Failure paths", "iOS surface" and "Testing".

## Global Constraints

- Repo: `/Users/david/visualstudio/bellasreef-ios`. Work in an isolated worktree on a branch off `main`; never on `main`.
- Swift 6.2, strict concurrency complete, iOS 26 floor. The API client is generated; never hand-write bindings. The only hand-written transport is `StreamClient`.
- Pulse constants, verbatim from the spec: duty **0.50**, transition **snap**, duration **5.0 s**, reason **"identify"**. Rebuild wait timeout **45 s**. Do not make any of them operator inputs.
- The bind for Identify sends **no `display_name`** (nil, so the key is absent on the wire). Role `light`, `device_id` `<driver>-<channel>` exactly as the sheet proposes today.
- "Not this one" issues `unbind`, then `forget` **only if** the bind returned `created: true`. The forget is `HubClient.forget(deviceId:)`, never the parameterless `HubClient.forget()` which clears the local credential.
- The wait for the rebuild is a `StateFrame` for the new `device_id` whose `payload.emittedAt` is strictly newer than the frame held for that id before the bind (any frame when none was held). Never poll capabilities, never trust `bound_to`.
- No client timer ends the hold; the server does. The client only sleeps out the 5 s so the sheet can move to the answer step.
- Every error string that reaches a screen goes through `HumanError.describe` (Kit, `HumanError.swift`); CI's `scripts/no-raw-errors.sh` enforces it. Do not add markers.
- Colours from `Theme`: failures render `Theme.attention` (amber). Red is safety only.
- Vocabulary: "PWM ch n", never "LED n", never "dimmer". The channel number shown is `capability.channel` exactly as the row shows it.
- Copy is plain and specific. No em-dashes in UI text, comments or docs.
- Sensors do not identify: the Identify button exists only for actuator capabilities (`capability.source.rawValue != "w1-bus"`).
- Project is xcodegen-generated: this plan adds no targets and no files outside the Kit package and `BellasReef/Views`, so `project.yml` is untouched. Do not hand-edit `BellasReef.xcodeproj`.
- Kit tests live in `BellasReefKit/Tests/BellasReefKitTests`, Swift Testing (`import Testing`, `@Suite`, `@Test`, `#expect`). `StubTransport`, `CallLog` and `MemoryCredentials` are internal types declared in `PairingTests.swift` and are reused; `anyHub`, `json(_:)` and `stub(_:)` are file-private in each test file and are redeclared per file.
- Build and test with the CI shape (`.github/workflows/ci.yaml`), `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` exported, each step its own foreground command with a long timeout:
  ```
  xcodegen generate
  xcodebuild build -project BellasReef.xcodeproj -scheme BellasReef \
    -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
    -derivedDataPath build/DerivedData -skipPackagePluginValidation -skipMacroValidation
  (cd BellasReefKit && xcodebuild test -scheme BellasReefKit-Package \
    -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
    -derivedDataPath ../build/DerivedData -skipPackagePluginValidation -skipMacroValidation)
  diff -q Contracts/openapi.json BellasReefKit/Sources/BellasReefAPI/openapi.json
  bash scripts/no-raw-errors.sh
  ```
  Focused Kit runs while iterating: append `-only-testing:BellasReefKitTests/<SuiteName>` to the `xcodebuild test` line. Never run `BellasReefUITests`.
- Conventional commits. Every commit message ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BNfMThZQTKBdgXs6DNvWEK
  ```
- Do not push, do not open the PR. The controller does that after review.
- Scope: one operator, paired devices, private LAN.

---

### Task 1: `StateFrameSource` and `TankMonitor.nextFrame(for:newerThan:timeout:)`

**Files:**
- Create: `BellasReefKit/Sources/BellasReefKit/StateFrameSource.swift`
- Modify: `BellasReefKit/Sources/BellasReefKit/TankMonitor.swift` (stored properties near `channels`, line 117; the `.state` case of `apply(_:)`, lines 391-402; new methods)
- Test: `BellasReefKit/Tests/BellasReefKitTests/IdentifyWaitTests.swift` (new)

**Interfaces:**
- Consumes: `TankMonitor.channels: [String: Components.Schemas.StateFrame]`, `TankMonitor.apply(_ frame: StreamFrame)` (internal), `StreamClient.decode(_:)` (internal, nonisolated).
- Produces:
  ```swift
  @MainActor public protocol StateFrameSource: AnyObject {
      func heldFrame(for deviceId: String) -> Components.Schemas.StateFrame?
      func nextFrame(for deviceId: String, newerThan floor: Date?, timeout: Duration) async -> Components.Schemas.StateFrame?
  }
  extension TankMonitor: StateFrameSource
  func TankMonitor.isWaitingForFrame(for deviceId: String) -> Bool   // internal, tests only
  ```

- [ ] **Step 1: Write the failing tests**

Create `BellasReefKit/Tests/BellasReefKitTests/IdentifyWaitTests.swift`:

```swift
// Bella's Reef iOS — closed source.

import Foundation
import Testing

@testable import BellasReefKit

/// The rebuild wait behind Identify (C4): a frame for the new device id whose
/// emitted_at clears the floor taken before the bind. BR_STATE is retained
/// last-value and replayed on connect, so a re-adopted channel can show a
/// frame from its previous life; that frame must not count.
@MainActor
@Suite("Identify: waiting for a fresh state frame")
struct IdentifyWaitTests {
    private let hub = Hub(
        name: "Bella's Reef", baseURL: URL(string: "http://hub.invalid:8000")!, discovered: false
    )

    private func stateJSON(id: String, duty: Double, emittedAt: String) -> String {
        """
        {"frame_version":1,"received_at":"2026-09-04T18:00:00.000000Z","kind":"state",\
        "subject":"bellasreef.state.\(id)","payload":{"schema_version":2,\
        "message_id":"\(UUID().uuidString.lowercased())","emitted_at":"\(emittedAt)",\
        "source":"hardware-io","actuator_id":"\(id)","level":{"kind":"pwm","duty":\(duty)},\
        "reason":"startup","since":"\(emittedAt)","latched":false},"override":null}
        """
    }

    private func monitor() -> (TankMonitor, StreamClient) {
        let client = HubClient(
            hub: hub, tokens: MemoryCredentials(token: "t"),
            transport: StubTransport { _, _, _ in (500, nil) }
        )
        let stream = StreamClient(baseURL: hub.baseURL)
        return (TankMonitor(client: client, stream: stream), stream)
    }

    private func duty(_ frame: Components.Schemas.StateFrame?) -> Double? {
        guard case let .pwm(level)? = frame?.payload.level else { return nil }
        return level.duty
    }

    private let t0 = "2026-09-04T17:00:00.000000Z"
    private let t0Date = ISO8601DateFormatter.withFractionalSeconds.date(from: "2026-09-04T17:00:00.000000Z")!
    private let t1 = "2026-09-04T17:00:20.000000Z"

    @Test("a held frame newer than the floor resolves at once")
    func heldNewerResolvesImmediately() async throws {
        let (m, s) = monitor()
        m.apply(try s.decode(stateJSON(id: "pca9685-3", duty: 0.0, emittedAt: t1)))
        let frame = await m.nextFrame(for: "pca9685-3", newerThan: t0Date, timeout: .seconds(1))
        #expect(duty(frame) == 0.0)
        #expect(!m.isWaitingForFrame(for: "pca9685-3"))
    }

    @Test("a retained frame at or before the floor does not satisfy the wait")
    func retainedOlderDoesNotSatisfy() async throws {
        let (m, s) = monitor()
        m.apply(try s.decode(stateJSON(id: "pca9685-3", duty: 0.7, emittedAt: t0)))
        let frame = await m.nextFrame(for: "pca9685-3", newerThan: t0Date, timeout: .milliseconds(30))
        #expect(frame == nil)
    }

    @Test("a live frame that clears the floor resolves a pending wait")
    func liveFrameResolves() async throws {
        let (m, s) = monitor()
        m.apply(try s.decode(stateJSON(id: "pca9685-3", duty: 0.7, emittedAt: t0)))
        let pending = Task { await m.nextFrame(for: "pca9685-3", newerThan: t0Date, timeout: .seconds(5)) }
        while !m.isWaitingForFrame(for: "pca9685-3") { await Task.yield() }
        m.apply(try s.decode(stateJSON(id: "pca9685-3", duty: 0.0, emittedAt: t1)))
        let frame = await pending.value
        #expect(duty(frame) == 0.0)
        #expect(!m.isWaitingForFrame(for: "pca9685-3"))
    }

    @Test("a live frame for another id does not resolve the wait")
    func otherIdIsIgnored() async throws {
        let (m, s) = monitor()
        let pending = Task { await m.nextFrame(for: "pca9685-3", newerThan: nil, timeout: .milliseconds(60)) }
        while !m.isWaitingForFrame(for: "pca9685-3") { await Task.yield() }
        m.apply(try s.decode(stateJSON(id: "pca9685-4", duty: 0.0, emittedAt: t1)))
        let frame = await pending.value
        #expect(frame == nil)
    }

    @Test("no frame by the timeout resolves nil and leaves no waiter behind")
    func timeoutResolvesNil() async {
        let (m, _) = monitor()
        let frame = await m.nextFrame(for: "never", newerThan: nil, timeout: .milliseconds(20))
        #expect(frame == nil)
        #expect(!m.isWaitingForFrame(for: "never"))
    }

    @Test("with no floor, any frame counts")
    func nilFloorAcceptsAnyFrame() async throws {
        let (m, s) = monitor()
        m.apply(try s.decode(stateJSON(id: "pca9685-3", duty: 0.7, emittedAt: t0)))
        let frame = await m.nextFrame(for: "pca9685-3", newerThan: nil, timeout: .seconds(1))
        #expect(duty(frame) == 0.7)
    }
}
```

If `ISO8601DateFormatter.withFractionalSeconds` does not exist as a static in this codebase, replace the `t0Date` line with:

```swift
    private var t0Date: Date {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: "2026-09-04T17:00:00.000000Z")!
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd BellasReefKit && xcodebuild test -scheme BellasReefKit-Package \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath ../build/DerivedData -skipPackagePluginValidation -skipMacroValidation \
  -only-testing:BellasReefKitTests/IdentifyWaitTests
```

Expected: compile failure, `value of type 'TankMonitor' has no member 'nextFrame'` and `'isWaitingForFrame'`.

- [ ] **Step 3: Write the protocol**

Create `BellasReefKit/Sources/BellasReefKit/StateFrameSource.swift`:

```swift
// Bella's Reef iOS — closed source.

import BellasReefAPI
import Foundation

/// Where Identify (C4) waits for proof that hardware-io rebuilt a channel.
///
/// Adopting restarts hardware-io. The last thing its rebuild does is publish
/// one startup `ActuatorState` per registered actuator, so a state frame for
/// the new device id proves the channel was built, opened and registered,
/// not merely that a process came back. A protocol rather than `TankMonitor`
/// itself so the flow's tests hand it canned frames without a socket.
@MainActor
public protocol StateFrameSource: AnyObject {
    /// The frame currently held for `deviceId`, if any. Its `emittedAt` is
    /// the floor a fresh frame must clear: BR_STATE is retained last-value
    /// and the hub replays it on connect, so a re-adopted channel can show a
    /// frame from its previous life.
    func heldFrame(for deviceId: String) -> Components.Schemas.StateFrame?

    /// The first frame for `deviceId` whose `payload.emittedAt` is strictly
    /// newer than `floor` (any frame when `floor` is nil), or nil once
    /// `timeout` passes. A held frame that already clears the floor resolves
    /// at once. Both timestamps come from the hub: one clock.
    func nextFrame(
        for deviceId: String, newerThan floor: Date?, timeout: Duration
    ) async -> Components.Schemas.StateFrame?
}
```

- [ ] **Step 4: Add the waiter registry and the primitive to `TankMonitor`**

In `TankMonitor.swift`, directly after the `channels` declaration (line 117), add:

```swift
    /// Pending `nextFrame(for:newerThan:timeout:)` calls, keyed by actuator
    /// id. Served from `apply(_:)` the moment a frame clears a waiter's floor.
    private struct FrameWaiter {
        let id: UUID
        let floor: Date?
        let deliver: @MainActor (Components.Schemas.StateFrame) -> Void
    }
    @ObservationIgnored private var frameWaiters: [String: [FrameWaiter]] = [:]
```

In `apply(_:)`, replace the `.state` case body:

```swift
        case let .state(state):
            connection = .live
            // Never regress. The hub replays each actuator's last known state
            // on connect and then joins the live fan-out; a change that lands
            // in between arrives *after* its own replayed predecessor. Keep
            // the newer by `emitted_at` — a stale frame must not overwrite a
            // fresh one (H3, 2026-08-18).
            let id = state.payload.actuatorId
            if let held = channels[id], held.payload.emittedAt > state.payload.emittedAt {
                return
            }
            channels[id] = state
            serveFrameWaiters(for: id, with: state)
```

Add these members to the class (after `apply(_:)` is fine):

```swift
    private func serveFrameWaiters(for id: String, with frame: Components.Schemas.StateFrame) {
        guard let waiting = frameWaiters[id], !waiting.isEmpty else { return }
        var kept: [FrameWaiter] = []
        for waiter in waiting {
            if Self.clears(frame, waiter.floor) { waiter.deliver(frame) } else { kept.append(waiter) }
        }
        frameWaiters[id] = kept.isEmpty ? nil : kept
    }

    private static func clears(_ frame: Components.Schemas.StateFrame, _ floor: Date?) -> Bool {
        guard let floor else { return true }
        return frame.payload.emittedAt > floor
    }

    // Internal, not private: the Identify wait tests need to know the waiter
    // is registered before they feed the frame that should resolve it.
    func isWaitingForFrame(for deviceId: String) -> Bool {
        !(frameWaiters[deviceId] ?? []).isEmpty
    }
```

Add the conformance as an extension at the bottom of `TankMonitor.swift`:

```swift
extension TankMonitor: StateFrameSource {
    public func heldFrame(for deviceId: String) -> Components.Schemas.StateFrame? {
        channels[deviceId]
    }

    public func nextFrame(
        for deviceId: String, newerThan floor: Date?, timeout: Duration
    ) async -> Components.Schemas.StateFrame? {
        if let held = channels[deviceId], Self.clears(held, floor) { return held }
        let (frames, continuation) = AsyncStream<Components.Schemas.StateFrame>.makeStream(
            bufferingPolicy: .bufferingNewest(1)
        )
        let token = UUID()
        frameWaiters[deviceId, default: []].append(
            FrameWaiter(id: token, floor: floor) { frame in
                continuation.yield(frame)
                continuation.finish()
            }
        )
        defer {
            frameWaiters[deviceId]?.removeAll { $0.id == token }
            if frameWaiters[deviceId]?.isEmpty == true { frameWaiters[deviceId] = nil }
        }
        // Race the stream against the clock. Whichever child finishes first
        // wins; the other is cancelled (AsyncStream iteration and Task.sleep
        // both return promptly on cancellation).
        return await withTaskGroup(of: Components.Schemas.StateFrame?.self) { group in
            group.addTask {
                for await frame in frames { return frame }
                return nil
            }
            group.addTask {
                try? await Task.sleep(for: timeout)
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            continuation.finish()
            return first
        }
    }
}
```

`clears(_:_:)` must be visible to the extension: keep it `private static` inside the class body only if the extension is in the same file (it is), otherwise make it `fileprivate`.

- [ ] **Step 5: Run the tests to verify they pass**

Same command as Step 2. Expected: 6 tests pass. Also run the neighbouring suite that feeds `apply(_:)`, to prove the `.state` path still orders frames:

```bash
... -only-testing:BellasReefKitTests/StateOrderingTests
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add BellasReefKit/Sources/BellasReefKit/StateFrameSource.swift \
        BellasReefKit/Sources/BellasReefKit/TankMonitor.swift \
        BellasReefKit/Tests/BellasReefKitTests/IdentifyWaitTests.swift
git commit -m "feat(kit): TankMonitor.nextFrame waits for a fresh state frame (C4)

The Identify flow needs proof that hardware-io rebuilt a channel after an
adopt. The proof is a state frame for the new device id whose emitted_at
clears the frame held before the bind, because BR_STATE is retained and
replayed on connect. One primitive on TankMonitor behind a StateFrameSource
protocol, so the flow's tests never touch a socket.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BNfMThZQTKBdgXs6DNvWEK"
```

---

### Task 2: `IdentifyFlow` phase machine in Kit

**Files:**
- Create: `BellasReefKit/Sources/BellasReefKit/IdentifyFlow.swift`
- Test: `BellasReefKit/Tests/BellasReefKitTests/IdentifyFlowTests.swift` (new)

**Interfaces:**
- Consumes (all existing on `HubClient`, `HubClient.swift`):
  ```swift
  func bind(_ request: Components.Schemas.BindDeviceRequest) async throws -> BindOutcome   // .bound(deviceId:created:) | .channelGone | .alreadyBound | .roleNotLegal
  func hold(target: String, duty: Double, durationS: Double, reason: String, transition: HoldTransition) async throws -> HoldOutcome   // .granted(OverrideView) | .notCommandable | .clockUntrusted
  func release(overrideId: String) async throws -> ReleaseOutcome
  func rename(deviceId: String, to name: String?) async throws
  func unbind(deviceId: String) async throws -> UnbindOutcome
  func forget(deviceId: String) async throws -> ForgetDeviceOutcome
  ```
  and Task 1's `StateFrameSource`. `Components.Schemas.OverrideView.id` is `String`.
- Produces:
  ```swift
  @MainActor @Observable public final class IdentifyFlow {
      public enum Step: Equatable, Sendable { case waitForHub, pulse, name, leave }
      public enum Phase: Equatable, Sendable {
          case choose, adopting, pulsing, answer, naming, named, left
          case failed(reason: String, retry: Step)
      }
      public static let pulseDuty = 0.50
      public static let pulseDurationS = 5.0
      public static let rebuildTimeout: Duration = .seconds(45)
      public private(set) var phase: Phase
      public private(set) var adopted: Bool
      public private(set) var created: Bool?
      public let deviceId: String
      public let channelLabel: String            // "PWM ch <channel>"
      public init(client: HubClient, frames: any StateFrameSource, request: Components.Schemas.BindDeviceRequest, channel: String, rebuildTimeout: Duration = IdentifyFlow.rebuildTimeout, pulseSettle: Duration = .seconds(5))
      public func start() async throws -> HubClient.BindOutcome
      public func pulseAgain()
      public func chooseToName()
      public func name(_ name: String) async
      public func leave()
      public func retry()
      func settle() async                        // internal, tests: awaits the running step
  }
  ```

- [ ] **Step 1: Write the failing tests**

Create `BellasReefKit/Tests/BellasReefKitTests/IdentifyFlowTests.swift`:

```swift
// Bella's Reef iOS — closed source.

import Foundation
import Testing

@testable import BellasReefKit

private let anyHub = Hub(
    name: "Bella's Reef", baseURL: URL(string: "http://hub.invalid:8000")!, discovered: false
)

private func json(_ text: String) -> Data { Data(text.utf8) }

/// Request bodies by operation id, so a test can assert what went on the wire.
/// A lock-protected class rather than an actor: `[String: Any]` is not
/// Sendable, so it must not cross an actor boundary (same idiom as
/// `CapturedBody` in AdoptionTests).
private final class Bodies: @unchecked Sendable {
    private let lock = NSLock()
    private var byOperation: [String: [Data]] = [:]
    func record(_ operation: String, _ body: Data) {
        lock.withLock { byOperation[operation, default: []].append(body) }
    }
    func last(_ operation: String) -> [String: Any]? {
        guard let data = lock.withLock({ byOperation[operation]?.last }), !data.isEmpty else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}

/// A frame source the test scripts: what is held before the bind, and what
/// the wait resolves to. Records the floor the flow asked for.
@MainActor
private final class CannedFrames: StateFrameSource {
    var held: Components.Schemas.StateFrame?
    var next: Components.Schemas.StateFrame?
    private(set) var askedFloor: Date??
    func heldFrame(for deviceId: String) -> Components.Schemas.StateFrame? { held }
    func nextFrame(
        for deviceId: String, newerThan floor: Date?, timeout: Duration
    ) async -> Components.Schemas.StateFrame? {
        askedFloor = .some(floor)
        return next
    }
}

private func stateFrame(emittedAt: String) throws -> Components.Schemas.StateFrame {
    let text = """
        {"frame_version":1,"received_at":"2026-09-04T18:00:00.000000Z","kind":"state",\
        "subject":"bellasreef.state.pca9685-3","payload":{"schema_version":2,\
        "message_id":"\(UUID().uuidString.lowercased())","emitted_at":"\(emittedAt)",\
        "source":"hardware-io","actuator_id":"pca9685-3","level":{"kind":"pwm","duty":0.0},\
        "reason":"startup","since":"\(emittedAt)","latched":false},"override":null}
        """
    guard case let .state(frame) = try StreamClient(baseURL: anyHub.baseURL).decode(text) else {
        throw ClientError.unexpected("not a state frame")
    }
    return frame
}

private let boundCreated = #"{"device_id":"pca9685-3","created":true,"driver_type":"pca9685","channel":"3"}"#
private let boundMatched = #"{"device_id":"pca9685-3","created":false,"driver_type":"pca9685","channel":"3"}"#
private let granted = #"{"id":"6f1c2a4e-0000-4000-8000-000000000001","target":"pca9685-3","duty":0.5,"expires_at":"2026-09-04T18:00:05Z","expires_in_s":5.0,"transition":"snap"}"#
private let renamed = #"{"device_id":"pca9685-3","display_name":"Left fixture"}"#

/// A hub that answers every call the flow can make; `overrideStatus` lets one
/// test refuse the pulse.
private func hub(
    log: CallLog, bodies: Bodies, bind: String, overrideStatus: Int = 200
) -> HubClient {
    HubClient(
        hub: anyHub, tokens: MemoryCredentials(token: "refresh"),
        transport: StubTransport { operation, _, body in
            if operation == "mintToken" {
                return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
            }
            await log.record(operation)
            bodies.record(operation, body)
            switch operation {
            case "bindDevice": return (200, json(bind))
            case "createOverride": return (overrideStatus, overrideStatus == 200 ? json(granted) : nil)
            case "renameDevice": return (200, json(renamed))
            case "unbindDevice", "forgetDevice": return (204, nil)
            case "releaseOverride": return (204, nil)
            default: return (500, nil)
            }
        }
    )
}

private func request() -> Components.Schemas.BindDeviceRequest {
    .init(channel: "3", deviceId: "pca9685-3", driverType: .pca9685, role: .light)
}

@MainActor
@Suite("Identify flow (C4)")
struct IdentifyFlowTests {
    private func makeFlow(
        _ client: HubClient, frames: CannedFrames, timeout: Duration = .seconds(1)
    ) -> IdentifyFlow {
        IdentifyFlow(
            client: client, frames: frames, request: request(), channel: "3",
            rebuildTimeout: timeout, pulseSettle: .milliseconds(1)
        )
    }

    @Test("happy path: adopt nameless, wait, pulse at 50 % for 5 s, name")
    func happyPath() async throws {
        let log = CallLog(), bodies = Bodies()
        let frames = CannedFrames()
        frames.next = try stateFrame(emittedAt: "2026-09-04T17:00:20.000000Z")
        let flow = makeFlow(hub(log: log, bodies: bodies, bind: boundCreated), frames: frames)

        #expect(flow.phase == .choose)
        #expect(flow.channelLabel == "PWM ch 3")
        let outcome = try await flow.start()
        #expect(outcome == .bound(deviceId: "pca9685-3", created: true))
        await flow.settle()
        #expect(flow.phase == .answer)
        #expect(flow.adopted)
        #expect(flow.created == true)
        #expect(frames.askedFloor == .some(nil), "nothing held, so any frame counts")

        flow.chooseToName()
        #expect(flow.phase == .naming)
        await flow.name("Left fixture")
        #expect(flow.phase == .named)

        #expect(await log.operations == ["bindDevice", "createOverride", "renameDevice"])
        let bind = bodies.last("bindDevice")
        #expect(bind?["display_name"] == nil, "identify adopts nameless")
        #expect(bind?["device_id"] as? String == "pca9685-3")
        #expect(bind?["role"] as? String == "light")
        let pulse = bodies.last("createOverride")
        #expect(pulse?["target"] as? String == "pca9685-3")
        #expect(pulse?["duty"] as? Double == 0.5)
        #expect(pulse?["duration_s"] as? Double == 5.0)
        #expect(pulse?["transition"] as? String == "snap")
        #expect(pulse?["reason"] as? String == "identify")
        let rename = bodies.last("renameDevice")
        #expect(rename?["display_name"] as? String == "Left fixture")
    }

    @Test("the floor is the frame held before the bind")
    func floorIsTheHeldFrame() async throws {
        let log = CallLog(), bodies = Bodies()
        let frames = CannedFrames()
        let old = try stateFrame(emittedAt: "2026-09-04T17:00:00.000000Z")
        frames.held = old
        frames.next = try stateFrame(emittedAt: "2026-09-04T17:00:20.000000Z")
        let flow = makeFlow(hub(log: log, bodies: bodies, bind: boundMatched), frames: frames)
        _ = try await flow.start()
        await flow.settle()
        #expect(frames.askedFloor == .some(old.payload.emittedAt))
        #expect(flow.created == false)
    }

    @Test("pulse again repeats the override without another bind")
    func pulseAgain() async throws {
        let log = CallLog(), bodies = Bodies()
        let frames = CannedFrames()
        frames.next = try stateFrame(emittedAt: "2026-09-04T17:00:20.000000Z")
        let flow = makeFlow(hub(log: log, bodies: bodies, bind: boundCreated), frames: frames)
        _ = try await flow.start()
        await flow.settle()
        flow.pulseAgain()
        await flow.settle()
        #expect(flow.phase == .answer)
        #expect(await log.count(of: "bindDevice") == 1)
        #expect(await log.count(of: "createOverride") == 2)
    }

    @Test("not this one on a matched row unbinds and does not forget")
    func notThisOneMatched() async throws {
        let log = CallLog(), bodies = Bodies()
        let frames = CannedFrames()
        frames.next = try stateFrame(emittedAt: "2026-09-04T17:00:20.000000Z")
        let flow = makeFlow(hub(log: log, bodies: bodies, bind: boundMatched), frames: frames)
        _ = try await flow.start()
        await flow.settle()
        flow.leave()
        await flow.settle()
        #expect(flow.phase == .left)
        #expect(await log.count(of: "unbindDevice") == 1)
        #expect(await log.count(of: "forgetDevice") == 0, "the row predates this flow; forgetting it deletes a device the operator built")
    }

    @Test("not this one on a created row unbinds then forgets")
    func notThisOneCreated() async throws {
        let log = CallLog(), bodies = Bodies()
        let frames = CannedFrames()
        frames.next = try stateFrame(emittedAt: "2026-09-04T17:00:20.000000Z")
        let flow = makeFlow(hub(log: log, bodies: bodies, bind: boundCreated), frames: frames)
        _ = try await flow.start()
        await flow.settle()
        flow.leave()
        await flow.settle()
        #expect(flow.phase == .left)
        #expect(await log.operations.suffix(2) == ["unbindDevice", "forgetDevice"])
    }

    @Test("an untrusted clock fails the pulse and leaves the adoption standing")
    func clockUntrusted() async throws {
        let log = CallLog(), bodies = Bodies()
        let frames = CannedFrames()
        frames.next = try stateFrame(emittedAt: "2026-09-04T17:00:20.000000Z")
        let flow = makeFlow(hub(log: log, bodies: bodies, bind: boundCreated, overrideStatus: 503), frames: frames)
        _ = try await flow.start()
        await flow.settle()
        guard case .failed(_, .pulse) = flow.phase else {
            Issue.record("expected .failed(retry: .pulse), got \(flow.phase)")
            return
        }
        #expect(flow.adopted)
        #expect(await log.count(of: "unbindDevice") == 0)
    }

    @Test("no frame within the timeout fails the wait; retry waits again")
    func rebuildTimeout() async throws {
        let log = CallLog(), bodies = Bodies()
        let frames = CannedFrames()
        frames.next = nil
        let flow = makeFlow(hub(log: log, bodies: bodies, bind: boundCreated), frames: frames, timeout: .milliseconds(1))
        _ = try await flow.start()
        await flow.settle()
        guard case .failed(_, .waitForHub) = flow.phase else {
            Issue.record("expected .failed(retry: .waitForHub), got \(flow.phase)")
            return
        }
        #expect(await log.count(of: "createOverride") == 0, "a pulse into the restart window is silently wrong")
        #expect(flow.adopted)

        frames.next = try stateFrame(emittedAt: "2026-09-04T17:00:20.000000Z")
        flow.retry()
        await flow.settle()
        #expect(flow.phase == .answer)
        #expect(await log.count(of: "createOverride") == 1)
    }

    @Test("a refused bind returns to choose with nothing adopted")
    func bindRefused() async throws {
        let log = CallLog(), bodies = Bodies()
        let client = HubClient(
            hub: anyHub, tokens: MemoryCredentials(token: "refresh"),
            transport: StubTransport { operation, _, _ in
                if operation == "mintToken" {
                    return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
                }
                await log.record(operation)
                return (409, nil)
            }
        )
        let flow = makeFlow(client, frames: CannedFrames())
        let outcome = try await flow.start()
        await flow.settle()
        #expect(outcome == .alreadyBound)
        #expect(flow.phase == .choose)
        #expect(!flow.adopted)
        #expect(await log.operations == ["bindDevice"])
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd BellasReefKit && xcodebuild test -scheme BellasReefKit-Package \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath ../build/DerivedData -skipPackagePluginValidation -skipMacroValidation \
  -only-testing:BellasReefKitTests/IdentifyFlowTests
```

Expected: compile failure, `cannot find 'IdentifyFlow' in scope`.

- [ ] **Step 3: Write `IdentifyFlow`**

Create `BellasReefKit/Sources/BellasReefKit/IdentifyFlow.swift`:

```swift
// Bella's Reef iOS — closed source.

import BellasReefAPI
import Foundation

/// Identify before adopt (C4, spec 2026-09-03).
///
/// Adopt a channel with no name, wait for hardware-io to rebuild it, pulse it
/// through the ordinary override path, and let the operator say whether the
/// right fixture lit. The order lives here, not in the sheet, so it is
/// testable against `StubTransport` and a canned `StateFrameSource`; the
/// sheet renders `phase` and calls the verbs.
///
/// Nothing here drives an unadopted channel: the pulse is a manual hold on a
/// device the hub has already registered with the full safety triple.
@MainActor
@Observable
public final class IdentifyFlow {
    /// The step a failure came from, which is what Retry repeats.
    public enum Step: Equatable, Sendable {
        case waitForHub
        case pulse
        case name
        case leave
    }

    public enum Phase: Equatable, Sendable {
        case choose
        /// Bound, waiting for the rebuild's startup frame.
        case adopting
        /// The hold is placed; the operator is watching the tank.
        case pulsing
        case answer
        case naming
        /// Named. The sheet dismisses and refreshes.
        case named
        /// Unbound (and forgotten if this flow created the row). The sheet
        /// dismisses and refreshes: hardware-io restarted on the way.
        case left
        case failed(reason: String, retry: Step)
    }

    /// Pinned by the spec. Duty clear of the 8 % floor and plainly visible;
    /// the 50 % row both silicons were metered at (1.654 V). Snap because the
    /// operator is standing at the tank. The server ends the hold.
    public static let pulseDuty = 0.50
    public static let pulseDurationS = 5.0
    /// Three times the measured hardware-io restart (about 15 s).
    public static let rebuildTimeout: Duration = .seconds(45)

    public private(set) var phase: Phase = .choose
    /// True once the hub holds a row for this channel that this flow bound.
    public private(set) var adopted = false
    /// From the bind. false means the hub matched a detached row that already
    /// carried a name and history; Not this one must not forget that row.
    public private(set) var created: Bool?
    public let deviceId: String
    /// "PWM ch n", the number the tapped row shows.
    public let channelLabel: String

    private let client: HubClient
    private let frames: any StateFrameSource
    private let request: Components.Schemas.BindDeviceRequest
    private let timeout: Duration
    private let pulseSettle: Duration
    private var floor: Date?
    private var activeHoldId: String?
    private var running: Task<Void, Never>?

    public init(
        client: HubClient,
        frames: any StateFrameSource,
        request: Components.Schemas.BindDeviceRequest,
        channel: String,
        rebuildTimeout: Duration = IdentifyFlow.rebuildTimeout,
        pulseSettle: Duration = .seconds(5)
    ) {
        precondition(request.displayName == nil, "identify adopts nameless; the name comes last")
        self.client = client
        self.frames = frames
        self.request = request
        self.deviceId = request.deviceId
        self.channelLabel = "PWM ch \(channel)"
        self.timeout = rebuildTimeout
        self.pulseSettle = pulseSettle
    }

    /// Bind, then (on `.bound`) wait for the rebuild and pulse in the
    /// background. Any other outcome, or a thrown transport error, leaves the
    /// phase at `.choose` for the sheet's existing error rendering.
    public func start() async throws -> HubClient.BindOutcome {
        floor = frames.heldFrame(for: deviceId)?.payload.emittedAt
        phase = .adopting
        let outcome: HubClient.BindOutcome
        do {
            outcome = try await client.bind(request)
        } catch {
            phase = .choose
            throw error
        }
        guard case let .bound(_, created) = outcome else {
            phase = .choose
            return outcome
        }
        adopted = true
        self.created = created
        run { await self.waitThenPulse() }
        return outcome
    }

    public func pulseAgain() {
        run { await self.pulse() }
    }

    public func chooseToName() {
        phase = .naming
    }

    public func name(_ name: String) async {
        phase = .naming
        do {
            try await client.rename(deviceId: deviceId, to: name)
            phase = .named
        } catch {
            phase = .failed(reason: HumanError.describe(error), retry: .name)
        }
    }

    /// Not this one, and Cancel while adopting. Ends a hold still inside its
    /// five seconds, unbinds, and forgets only a row this flow created.
    public func leave() {
        running?.cancel()
        run { await self.unbindAndMaybeForget() }
    }

    public func retry() {
        guard case let .failed(_, step) = phase else { return }
        switch step {
        case .waitForHub: run { await self.waitThenPulse() }
        case .pulse: run { await self.pulse() }
        case .name: phase = .naming
        case .leave: leave()
        }
    }

    // Internal, not private: the tests await the background step.
    func settle() async {
        await running?.value
    }

    private func run(_ step: @escaping @MainActor () async -> Void) {
        running = Task { await step() }
    }

    private func waitThenPulse() async {
        phase = .adopting
        let frame = await frames.nextFrame(for: deviceId, newerThan: floor, timeout: timeout)
        if Task.isCancelled { return }
        guard frame != nil else {
            phase = .failed(
                reason: "The hub is still restarting. Retry waits for it again; the channel stays adopted.",
                retry: .waitForHub
            )
            return
        }
        await pulse()
    }

    private func pulse() async {
        phase = .pulsing
        do {
            switch try await client.hold(
                target: deviceId, duty: Self.pulseDuty, durationS: Self.pulseDurationS,
                reason: "identify", transition: .snap
            ) {
            case let .granted(view):
                activeHoldId = view.id
                // The server expires the hold; this only paces the sheet to
                // the answer step. A backgrounded app arrives there on return.
                try? await Task.sleep(for: pulseSettle)
                if Task.isCancelled { return }
                activeHoldId = nil
                phase = .answer
            case .clockUntrusted:
                phase = .failed(
                    reason: "The hub's clock is still syncing. Try Identify again in a moment.",
                    retry: .pulse
                )
            case .notCommandable:
                phase = .failed(reason: "The hub refused the pulse on this channel.", retry: .pulse)
            }
        } catch {
            phase = .failed(reason: HumanError.describe(error), retry: .pulse)
        }
    }

    private func unbindAndMaybeForget() async {
        do {
            if let holdId = activeHoldId {
                // Tolerated either way: 404 means the hold already expired.
                _ = try? await client.release(overrideId: holdId)
                activeHoldId = nil
            }
            if adopted {
                _ = try await client.unbind(deviceId: deviceId)
                if created == true {
                    _ = try await client.forget(deviceId: deviceId)
                }
            }
            phase = .left
        } catch {
            phase = .failed(reason: HumanError.describe(error), retry: .leave)
        }
    }
}
```

If `HubClient.BindOutcome`'s `.bound` associated values are labelled (`.bound(deviceId:created:)`), the pattern `case let .bound(_, created)` binds positionally and compiles; keep it.

- [ ] **Step 4: Run the tests to verify they pass**

Same command as Step 2. Expected: 8 tests pass, no warnings from the new files.

- [ ] **Step 5: Commit**

```bash
git add BellasReefKit/Sources/BellasReefKit/IdentifyFlow.swift \
        BellasReefKit/Tests/BellasReefKitTests/IdentifyFlowTests.swift
git commit -m "feat(kit): IdentifyFlow, the identify-before-adopt phase machine (C4)

Bind nameless, wait for the rebuild's startup frame, pulse at 50 % for 5 s
through the ordinary override path, then name the device or unbind it,
forgetting only a row this flow created. The order is in Kit so it is tested
against StubTransport and canned frames; the sheet only renders phases.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BNfMThZQTKBdgXs6DNvWEK"
```

---

### Task 3: The adopt sheet renders the flow

**Files:**
- Modify: `BellasReef/Views/AdoptDeviceSheet.swift` (whole file; the current body is quoted below where it changes)
- No change: `BellasReef/Views/SystemView.swift` (its `AdoptDeviceSheet(capability:) { Task { await loadHardware() } }` call is unchanged; the sheet calls `onAdopted()` on both `.named` and `.left`, since hardware-io restarted either way).

**Interfaces:**
- Consumes: Task 2's `IdentifyFlow` (`phase`, `channelLabel`, `created`, `start()`, `pulseAgain()`, `chooseToName()`, `name(_:)`, `leave()`, `retry()`), `AppModel.client: HubClient?`, `AppModel.monitor: TankMonitor?` (a `StateFrameSource` since Task 1).
- Produces: nothing new for other tasks.

- [ ] **Step 1: Add the flow state and the two entry buttons**

In `AdoptDeviceSheet`, add state alongside the existing `@State` properties:

```swift
    /// Which button the safety confirm is standing in front of.
    private enum Pending { case adopt, identify }
    @State private var pending: Pending = .adopt
    /// Non-nil from the moment Identify is confirmed until the sheet closes.
    @State private var identify: IdentifyFlow?
    @State private var identifyName = ""
```

Replace the existing `Section { Button { ... "Adopt" ... } ... }` (the section holding the Adopt button and the `problem` label) with:

```swift
                Section {
                    if isActuator {
                        // Identify needs the stream to prove the rebuild;
                        // without a monitor the flow would time out every
                        // time, so the button is not offered.
                        Button {
                            pending = .identify
                            confirming = true
                        } label: {
                            if working && pending == .identify { ProgressView() } else { Text("Identify this channel") }
                        }
                        .frame(minHeight: 44)
                        .disabled(working || model.client == nil || model.monitor == nil)
                        .accessibilityIdentifier("adopt-identify-button")
                    }
                    Button {
                        pending = .adopt
                        if isActuator { confirming = true } else { Task { await adopt() } }
                    } label: {
                        if working && pending == .adopt { ProgressView() } else { Text(isActuator ? "Adopt without identifying" : "Adopt") }
                    }
                    .frame(minHeight: 44)
                    .disabled(
                        name.trimmingCharacters(in: .whitespaces).isEmpty
                            || working
                            || !pollIntervalValid
                    )
                    .accessibilityIdentifier("adopt-confirm-button")

                    if let problem {
                        Label(problem, systemImage: "exclamationmark.triangle.fill")
                            .font(Theme.caption)
                            .foregroundStyle(Theme.attention)
                    }
                } footer: {
                    if isActuator {
                        Text("Identify adopts the channel with no name, then holds it at 50 percent for 5 seconds so you can see which fixture it is.")
                    }
                }
```

In the `confirmationDialog`, change the confirm button so it branches on `pending`:

```swift
                Button(pending == .identify ? "Adopt and identify" : "Adopt", role: .destructive) {
                    Task { if pending == .identify { await startIdentify() } else { await adopt() } }
                }
```

- [ ] **Step 2: Add the identify phases to the form**

Wrap the existing `Form { ... }` content so that the Channel section always shows and the Device/buttons sections show only while `identify == nil`. Concretely, change the `Form` body to:

```swift
            Form {
                Section {
                    LabeledContent("Source", value: capability.source.rawValue)
                    LabeledContent("Channel", value: capability.channel)
                    LabeledContent("Driver", value: driverType.rawValue)
                } header: {
                    Text("Channel")
                }
                if let identify {
                    identifyPhases(identify)
                } else {
                    deviceSection
                    actionSection
                }
            }
```

where `deviceSection` is the existing `Section("Device") { ... }` moved verbatim into `private var deviceSection: some View { ... }`, and `actionSection` is Step 1's section moved into `private var actionSection: some View { ... }`. Keep the existing comments with the code they annotate.

Add the phase renderer:

```swift
    @ViewBuilder
    private func identifyPhases(_ flow: IdentifyFlow) -> some View {
        switch flow.phase {
        case .choose:
            // Only reachable for an instant: start() returned a refusal and
            // `problem` carries it, so fall back to the normal sections.
            deviceSection
            actionSection
        case .adopting:
            Section("Identify") {
                Label {
                    Text("Adopting the channel. The hub restarts to pick it up, about 15 seconds.")
                } icon: {
                    ProgressView()
                }
                Button("Cancel", role: .destructive) { flow.leave() }
                    .accessibilityIdentifier("identify-cancel-button")
            }
        case .pulsing:
            Section("Identify") {
                Label {
                    Text("Watch your fixtures. \(flow.channelLabel) is at 50 percent for 5 seconds.")
                } icon: {
                    Image(systemName: "sun.max.fill").foregroundStyle(Theme.accent)
                }
            }
        case .answer:
            Section("Identify") {
                Text("Did the right fixture light up?")
                Button("Yes, name it") { flow.chooseToName() }
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("identify-yes-button")
                Button("Pulse again") { flow.pulseAgain() }
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("identify-again-button")
                Button("Not this one", role: .destructive) { flow.leave() }
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("identify-no-button")
            }
        case .naming:
            Section("Name") {
                TextField("Name", text: $identifyName)
                    .accessibilityIdentifier("identify-name-field")
                Button {
                    Task { await flow.name(identifyName.trimmingCharacters(in: .whitespaces)) }
                } label: {
                    Text("Save")
                }
                .frame(minHeight: 44)
                .disabled(identifyName.trimmingCharacters(in: .whitespaces).isEmpty)
                .accessibilityIdentifier("identify-save-button")
            }
        case .named, .left:
            // The sheet is on its way out; see the onChange below.
            EmptyView()
        case let .failed(reason, step):
            Section("Identify") {
                Label(reason, systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.caption)
                    .foregroundStyle(Theme.attention)
                Button("Retry") { flow.retry() }
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("identify-retry-button")
                if step != .leave {
                    Button("Not this one", role: .destructive) { flow.leave() }
                        .frame(minHeight: 44)
                        .accessibilityIdentifier("identify-no-button")
                }
            }
        }
    }
```

- [ ] **Step 3: Start the flow, watch its phase, announce phase changes**

Add the starter next to `adopt()`:

```swift
    /// Identify: bind with no name, wait, pulse. Refusals land in `problem`
    /// exactly as `adopt()`'s do, with the sheet back on its normal sections.
    private func startIdentify() async {
        guard let client = model.client, let monitor = model.monitor else {
            problem = "Not connected to the hub."
            return
        }
        working = true
        defer { working = false }
        problem = nil
        let flow = IdentifyFlow(
            client: client,
            frames: monitor,
            request: .init(
                channel: capability.channel,
                deviceId: proposedDeviceId,
                driverType: driverType,
                role: .light
            ),
            channel: capability.channel
        )
        identify = flow
        do {
            switch try await flow.start() {
            case let .bound(_, created):
                // A matched row keeps its name; prefillDetachedName() already
                // put it in `name` when the catalog knew the row.
                identifyName = created ? "" : name
            case .channelGone:
                identify = nil
                problem = "The hub no longer announces this channel. Pull to refresh the list."
            case .alreadyBound:
                identify = nil
                problem = "Another device claimed this channel since the list loaded."
            case .roleNotLegal:
                identify = nil
                problem = "The hub refused the role for this device."
            }
        } catch {
            identify = nil
            problem = HumanError.describe(error)
        }
    }
```

Attach to the `NavigationStack` (next to the existing `.task { ... }`):

```swift
        .onChange(of: identify?.phase) { _, phase in
            guard let phase else { return }
            switch phase {
            case .named, .left:
                onAdopted()
                dismiss()
            case .pulsing:
                AccessibilityNotification.Announcement("Pulsing \(identify?.channelLabel ?? "the channel")").post()
            case .answer:
                AccessibilityNotification.Announcement("Pulse finished. Did the right fixture light up?").post()
            case let .failed(reason, _):
                AccessibilityNotification.Announcement(reason).post()
            case .choose, .adopting, .naming:
                break
            }
        }
```

`IdentifyFlow.Phase` is `Equatable`, which `onChange` needs. `AccessibilityNotification` is SwiftUI (iOS 17+); no import beyond `SwiftUI` is needed.

The toolbar Cancel must not abandon a half-done identify: change it to

```swift
                    Button("Cancel") {
                        if let identify, identify.adopted, identify.phase != .named, identify.phase != .left {
                            identify.leave()
                        } else {
                            dismiss()
                        }
                    }
```

- [ ] **Step 4: Build and run the CI shape**

From the repo root, each as its own command:

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
xcodegen generate
xcodebuild build -project BellasReef.xcodeproj -scheme BellasReef \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath build/DerivedData -skipPackagePluginValidation -skipMacroValidation
(cd BellasReefKit && xcodebuild test -scheme BellasReefKit-Package \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath ../build/DerivedData -skipPackagePluginValidation -skipMacroValidation)
diff -q Contracts/openapi.json BellasReefKit/Sources/BellasReefAPI/openapi.json
bash scripts/no-raw-errors.sh
```

Expected: build succeeds with no new warnings in the touched files; all Kit tests pass; the diff prints nothing; no-raw-errors exits 0.

If the booted simulator is paired to a hub, install and launch the app, open System, tap an available PWM channel, and take one screenshot of the sheet showing the Identify button (`xcrun simctl io booted screenshot identify-choose.png` into the plan workspace). Do not confirm the safety dialog against a real hub; the bench pass is David's. If the simulator is not paired, skip and say so.

- [ ] **Step 5: Commit**

```bash
git add BellasReef/Views/AdoptDeviceSheet.swift
git commit -m "feat(adopt): Identify this channel, the C4 flow in the adopt sheet

The adopt sheet gains a phase machine instead of a second screen: Identify
adopts the channel nameless, waits for the hub's rebuild, holds it at 50 %
for 5 s, and asks whether the right fixture lit. Yes names it; Pulse again
repeats the hold; Not this one unbinds and forgets only a row the flow
created. The safety confirm still stands in front of both buttons, and the
adopt-with-a-name path is unchanged. Phase changes are announced for
VoiceOver, since the pulse itself has no substitute.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BNfMThZQTKBdgXs6DNvWEK"
```

---

## Spec coverage check (controller's self-review)

| Spec section | Task |
|---|---|
| Flow steps 1-5 (pick, provisional adopt, wait, pulse, answer) | 2 (machine), 3 (sheet) |
| Forget guard on `created` | 2 (`unbindAndMaybeForget`), tested both ways |
| Provisional adoption is a name, not a state (no flag) | 2 (bind with `displayName` nil; precondition) |
| The pulse: 0.50 / snap / 5.0 / "identify"; server ends it; DELETE only for a cancel inside the window | 2 (`pulse`, `leave` releases an active hold) |
| Waiting for the rebuild: fresh `StateFrame`, `emittedAt` floor, 45 s, Retry waits again | 1 (primitive), 2 (`waitThenPulse`, `retry`) |
| Failure paths table | 2 (503, timeout, refusal), 3 (copy) |
| iOS surface phase table and copy | 3 |
| Vocabulary "PWM ch n" | 2 (`channelLabel`) |
| Accessibility announcements | 3 (`onChange`) |
| Testing, iOS kit list | 1 (retained-older test), 2 (happy, matched no-forget, created forget, 503, timeout) |
| Bench acceptance | controller, after merge and deploy (David's meter) |

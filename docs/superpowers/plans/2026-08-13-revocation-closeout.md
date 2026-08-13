# Revocation Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three findings from the 2026-08-13 live revoke testing: a revoked device should land on the pairing screen from its *first* interaction (not its second), the hub should kill a revoked device's WebSocket instead of streaming to it indefinitely, and concurrent access-token mints should coalesce into one.

**Architecture:** Two iOS changes in `BellasReefKit` (mint coalescing in the `HubClient` actor; a retry-once-through-mint in `BearerAuthMiddleware`) and one backend change (a time-gated `is_active` recheck in the `/api/v1/stream` send loop). No contract changes; no new endpoints; no migration. The combined effect: revoke a device from anywhere, and within ~10 s its stream closes, its next mint is rejected, and it lands on "Find your hub" with the revocation notice — untouched.

**Tech Stack:** Swift 6.2 (strict concurrency) / swift-openapi-runtime ClientMiddleware / Swift Testing (`import Testing`) in `bellasreef-ios`; Python 3.13 / FastAPI WebSocket / pytest in `bellasreef`.

## Global Constraints

- Two repos: iOS tasks run in `/Users/david/visualstudio/bellasreef-ios`, backend task in `/Users/david/visualstudio/bellasreef`. Each task states its repo.
- iOS kit tests run via: `cd BellasReefKit && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild test -scheme BellasReefKit-Package -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -skipPackagePluginValidation -skipMacroValidation` (NOT `swift test` — the package's platform floor is iOS-only in practice).
- iOS pushes go directly to `main` (repo practice); backend changes go through a PR, CI green, merge, then `scripts/deploy-pi.sh` — a backend pass is not done at CI green (CLAUDE.md deployment discipline).
- Backend local gate before any push: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` (this Mac has no container runtime; the skip must be declared, and CI is where integration suites actually run).
- `mypy --strict` and ruff must stay clean on the backend; `SWIFT_STRICT_CONCURRENCY: complete` on iOS — no `@unchecked Sendable` beyond the existing idioms.
- Test doubles already exist in `BellasReefKit/Tests/BellasReefKitTests/PairingTests.swift`: `StubTransport` (`handle: (operationID, HTTPRequest, Data) -> (Int, Data?)`), `MemoryCredentials`, `CallLog` (actor recording operation IDs). They are internal to the test target — reuse, do not redefine. `CredentialRejectionTests.swift` has a file-private `Flag` class and its own file-private `anyHub`/`json` helpers; new test files need their own private copies of `anyHub`/`json` (they are `private` per file, three lines).
- Conventional commits, `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.

## Interfaces already in the codebase (read before starting)

- `HubClient` is an **actor** (`BellasReefKit/Sources/BellasReefKit/HubClient.swift`). Relevant existing members:
  - `public func accessTokenNow() async throws -> String` — returns cached token when >60 s from expiry, else mints via `client.mintToken`; on mint 401 it runs `try? tokens.clear()`, fires `credentialRejectedHandler?()`, and `throw credentialWasRejected()`.
  - `private var credentialRejectedHandler: (@Sendable () -> Void)?` + `public func notifyCredentialRejected(_:)` — added 2026-08-13, tested in `CredentialRejectionTests.swift`.
  - `private func credentialWasRejected() -> ClientError` — clears the cached access token, returns `.unauthorized`.
  - `init` builds `Client(... middlewares: [BearerAuthMiddleware(token: { try await provider.token() })])` and then `provider.resolve = { [self] in try await accessTokenNow() }`.
- `TokenProvider` (`BearerAuth.swift`): `final class`, NSLock-guarded `resolve` closure, `func token() async throws -> String`.
- `BearerAuthMiddleware` (`BearerAuth.swift`): struct, skips ops in `unauthenticated: Set<String> = ["info", "pair", "mintToken", "pollPairing", "healthz"]`, else attaches `Bearer` header and calls `next` once.
- Backend stream endpoint: `services/api/bellasreef_api/app.py:1673` — auth by first message, `store.is_active(client_id)` checked once at handshake, then `while True: await websocket.send_text(await queue.get())`. `is_active` lives at `services/api/bellasreef_api/store.py:517`, signature `async def is_active(self, client_id: UUID) -> bool`.
- Backend WS test idiom: `services/api/tests/test_stream_and_overrides.py` class `TestWebSocketStream` — `_app_and_token()` builds the app over a fresh engine with a real NATS (`_NATS` env), pairs a client via TOFU, mints a token; frames are published from a `threading.Thread` running `asyncio.run(...)` (portal-loop constraint documented in the file). These are integration tests, env-gated, checked in CI.

---

### Task 1: Coalesce concurrent mints (iOS kit)

Repo: `bellasreef-ios`. Two callers hitting `accessTokenNow()` while the cache is empty each start a mint — the actor suspends at `await client.mintToken`, letting the second caller in (observed live: two `token.minted` audit rows 38 µs apart). Coalesce: one mint in flight, later callers await it.

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/HubClient.swift` (the `accessTokenNow()` region, ~line 390)
- Test: `BellasReefKit/Tests/BellasReefKitTests/MintCoalescingTests.swift` (create)

**Interfaces:**
- Consumes: `StubTransport`, `MemoryCredentials`, `CallLog` from `PairingTests.swift`.
- Produces: `accessTokenNow()` semantics unchanged to callers; adds private `mintInFlight: Task<String, any Error>?` and private `mintFresh()`. Task 2 builds `freshAccessTokenNow()` on top of these exact names.

- [ ] **Step 1: Write the failing test**

```swift
// Bella's Reef iOS — closed source.

import Foundation
import Testing

@testable import BellasReefKit

private let anyHub = Hub(
    name: "Bella's Reef", baseURL: URL(string: "http://hub.invalid:8000")!, discovered: false
)

private func json(_ text: String) -> Data { Data(text.utf8) }

/// Observed live 2026-08-13: two `token.minted` audit rows 38 µs apart from
/// one device. The actor suspends across `await mintToken`, so a second
/// caller finds no cached token and starts a second mint.
@Suite("Mint coalescing")
struct MintCoalescingTests {

    @Test("two concurrent callers share one mint")
    func concurrentCallersCoalesce() async throws {
        let log = CallLog()
        let transport = StubTransport { operation, _, _ in
            await log.record(operation)
            // Long enough that the second caller arrives while the first
            // mint is on the wire.
            try await Task.sleep(for: .milliseconds(80))
            return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
        }
        let client = HubClient(
            hub: anyHub, tokens: MemoryCredentials(token: "refresh"), transport: transport
        )

        async let first = client.accessTokenNow()
        async let second = client.accessTokenNow()
        let (a, b) = try await (first, second)

        #expect(a == "jwt" && b == "jwt")
        #expect(await log.count(of: "mintToken") == 1, "concurrent callers must share one mint")
    }
}
```

- [ ] **Step 2: Run it to verify it fails**

Run (from `bellasreef-ios/BellasReefKit`): the Global Constraints xcodebuild line plus `-only-testing:BellasReefKitTests/MintCoalescingTests`.
Expected: FAIL — `count(of: "mintToken")` is 2.

- [ ] **Step 3: Implement coalescing in `HubClient`**

Replace the body of `accessTokenNow()` and add the two private members. The existing mint logic (including the 401 branch with `tokens.clear()`, `credentialRejectedHandler?()`, `credentialWasRejected()`) moves verbatim into `mintFresh()`:

```swift
    /// One mint at a time. The actor suspends across `await mintToken`, so
    /// without this a second caller finds no cached token and starts a second
    /// mint — observed live as two `token.minted` audit rows 38 µs apart.
    private var mintInFlight: Task<String, any Error>?

    /// A valid access token, minted if the cached one is missing or stale.
    ///
    /// Refreshed a minute early: a token that expires mid-request is a failure
    /// the operator sees, and a minute of margin costs nothing.
    public func accessTokenNow() async throws -> String {
        if let token = accessToken, let expiry = accessExpiry,
           expiry.timeIntervalSinceNow > 60 {
            return token
        }
        if let inFlight = mintInFlight { return try await inFlight.value }

        let work = Task { try await self.mintFresh() }
        mintInFlight = work
        defer { mintInFlight = nil }
        return try await work.value
    }

    private func mintFresh() async throws -> String {
        guard let refresh = try tokens.load() else { throw ClientError.unauthorized }

        let output = try await client.mintToken(body: .json(.init(refreshToken: refresh)))
        switch output {
        case let .ok(response):
            let minted = try response.body.json
            accessToken = minted.accessToken
            accessExpiry = Date().addingTimeInterval(TimeInterval(minted.expiresIn))
            return minted.accessToken
        case .unauthorized:
            // Revoked, or the hub was rebuilt. Forget the credential rather
            // than retrying against something that will never accept it.
            try? tokens.clear()
            credentialRejectedHandler?()
            throw credentialWasRejected()
        case let .undocumented(statusCode, _):
            throw ClientError.unexpected("token returned \(statusCode)")
        default:
            throw ClientError.unexpected("token returned an unhandled response")
        }
    }
```

(Keep whatever trailing members followed the old `accessTokenNow` — only the function body is restructured. `Task {}` inside an actor method inherits the actor, so `mintFresh()` needs no `await` hop.)

- [ ] **Step 4: Run the full kit suite**

Run: the Global Constraints xcodebuild line (no `-only-testing`).
Expected: all suites pass — the new test plus the existing 38, including both `CredentialRejectionTests` (their single-caller flows go through `mintFresh()` unchanged).

- [ ] **Step 5: Commit**

```bash
git add BellasReefKit/Sources/BellasReefKit/HubClient.swift \
        BellasReefKit/Tests/BellasReefKitTests/MintCoalescingTests.swift
git commit -m "fix(auth): concurrent access-token mints coalesce into one

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: A forced-fresh token path (iOS kit)

Repo: `bellasreef-ios`. The retry in Task 3 needs a token that is *known* fresh — "drop the cache, then mint" — exposed through `TokenProvider` so the middleware can reach it the same late-bound way it reaches `token()`.

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/HubClient.swift` (add one method; extend the `init` wiring)
- Modify: `BellasReefKit/Sources/BellasReefKit/BearerAuth.swift` (`TokenProvider` gains a second resolver)
- Test: `BellasReefKit/Tests/BellasReefKitTests/MintCoalescingTests.swift` (extend)

**Interfaces:**
- Consumes: `mintInFlight` / `mintFresh()` exactly as named in Task 1.
- Produces: `HubClient.freshAccessTokenNow() async throws -> String`; `TokenProvider.freshToken() async throws -> String` backed by `resolveFresh`. Task 3's middleware calls `freshToken()`.

- [ ] **Step 1: Write the failing test** (append to `MintCoalescingTests.swift`, inside the suite)

```swift
    @Test("freshAccessTokenNow ignores the cache but joins an in-flight mint")
    func forcedFreshMints() async throws {
        let log = CallLog()
        let transport = StubTransport { operation, _, _ in
            await log.record(operation)
            return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
        }
        let client = HubClient(
            hub: anyHub, tokens: MemoryCredentials(token: "refresh"), transport: transport
        )

        _ = try await client.accessTokenNow()          // mint 1, cached
        _ = try await client.accessTokenNow()          // cache hit, no mint
        _ = try await client.freshAccessTokenNow()     // must mint again
        #expect(await log.count(of: "mintToken") == 2, "forced fresh must not trust the cache")
    }
```

- [ ] **Step 2: Run it to verify it fails**

Expected: FAIL — `freshAccessTokenNow` does not exist (compile error is the failure).

- [ ] **Step 3: Implement**

In `HubClient.swift`, directly below `accessTokenNow()`:

```swift
    /// A token that is *known* fresh: the cache is dropped first, so the hub
    /// is consulted. This is what turns a data-call 401 into an answer — a
    /// stale token gets replaced, a revoked device gets `mintToken`'s
    /// rejection and the handler fires. Joins an in-flight mint rather than
    /// stacking a second one: that mint is fresh by definition.
    public func freshAccessTokenNow() async throws -> String {
        accessToken = nil
        accessExpiry = nil
        return try await accessTokenNow()
    }
```

In `BearerAuth.swift`, extend `TokenProvider` (same lock, same write-once pattern):

```swift
    private var _resolveFresh: (@Sendable () async throws -> String)?

    var resolveFresh: (@Sendable () async throws -> String)? {
        get { lock.withLock { _resolveFresh } }
        set { lock.withLock { _resolveFresh = newValue } }
    }

    func freshToken() async throws -> String {
        guard let resolveFresh else {
            throw HubClient.ClientError.unexpected("no credential is available yet")
        }
        return try await resolveFresh()
    }
```

In `HubClient.init`, next to the existing resolver line:

```swift
        provider.resolve = { [self] in try await accessTokenNow() }
        provider.resolveFresh = { [self] in try await freshAccessTokenNow() }
```

- [ ] **Step 4: Run the full kit suite** — expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add BellasReefKit/Sources/BellasReefKit/HubClient.swift \
        BellasReefKit/Sources/BellasReefKit/BearerAuth.swift \
        BellasReefKit/Tests/BellasReefKitTests/MintCoalescingTests.swift
git commit -m "feat(auth): a forced-fresh token path for the retry middleware

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Retry once through a fresh mint (iOS kit)

Repo: `bellasreef-ios`. The two-interaction landing, closed at the choke point: on a data-call 401, `BearerAuthMiddleware` gets a forced-fresh token and resends once. A stale token becomes invisible; a revoked device gets its mint rejection — and therefore the pairing screen — on the *first* interaction.

**Files:**
- Modify: `BellasReefKit/Sources/BellasReefKit/BearerAuth.swift` (middleware `intercept` + `HubClient.init` call site signature)
- Modify: `BellasReefKit/Sources/BellasReefKit/HubClient.swift` (init: pass `freshToken:`)
- Test: `BellasReefKit/Tests/BellasReefKitTests/RetryThroughMintTests.swift` (create)

**Interfaces:**
- Consumes: `TokenProvider.freshToken()` from Task 2.
- Produces: `BearerAuthMiddleware(token:freshToken:)` — both closures `@Sendable () async throws -> String`. No caller outside `HubClient.init` constructs it.

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

private final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var raised = false
    func raise() { lock.withLock { raised = true } }
    var isRaised: Bool { lock.withLock { raised } }
}

/// Found live 2026-08-13: a device revoked 16 s after minting took two
/// interactions to land on the pairing screen — the first burned the cached
/// token as an inline error, only the second forced the mint that proved the
/// revocation. The middleware now spends that first interaction properly.
@Suite("Retry through a fresh mint")
struct RetryThroughMintTests {

    /// The hub 401s a data call (stale access token) but mints happily. The
    /// operator must never see it.
    @Test("a stale access token is retried invisibly")
    func staleTokenRetriesInvisibly() async throws {
        let log = CallLog()
        let transport = StubTransport { operation, _, _ in
            await log.record(operation)
            if operation == "mintToken" {
                return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
            }
            // First data attempt 401s, the retry succeeds.
            if await log.count(of: operation) == 1 { return (401, nil) }
            return (200, json("[]"))
        }
        let client = HubClient(
            hub: anyHub, tokens: MemoryCredentials(token: "refresh"), transport: transport
        )
        let flag = Flag()
        await client.notifyCredentialRejected { flag.raise() }

        let sensors = try await client.sensors()

        #expect(sensors.isEmpty)
        #expect(await log.count(of: "listSensors") == 2, "one retry, exactly")
        #expect(await log.count(of: "mintToken") == 2, "the retry re-minted")
        #expect(!flag.isRaised, "a stale token is not a revocation")
    }

    /// A revoked device: the retry's mint is rejected, the handler fires,
    /// the call throws. One interaction, one landing.
    @Test("a revoked device is told on the first interaction")
    func revokedDeviceLandsFirstTry() async throws {
        let log = CallLog()
        let transport = StubTransport { operation, _, _ in
            await log.record(operation)
            if operation == "mintToken" {
                // First mint succeeds (the device does not know yet); the
                // forced re-mint meets the revocation.
                if await log.count(of: "mintToken") == 1 {
                    return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
                }
                return (401, nil)
            }
            return (401, nil)
        }
        let client = HubClient(
            hub: anyHub, tokens: MemoryCredentials(token: "refresh"), transport: transport
        )
        let flag = Flag()
        await client.notifyCredentialRejected { flag.raise() }

        await #expect(throws: HubClient.ClientError.self) {
            _ = try await client.sensors()
        }
        #expect(flag.isRaised, "the rejected re-mint must reach the app layer")
        #expect(await log.count(of: "listSensors") == 1, "no resend after a rejected mint")
    }

    /// Both attempts 401 while mints succeed (a hub-side authorization quirk,
    /// not a dead credential): give up after one retry, stay quiet.
    @Test("a persistent 401 is thrown after exactly one retry")
    func persistent401StopsAfterOneRetry() async throws {
        let log = CallLog()
        let transport = StubTransport { operation, _, _ in
            await log.record(operation)
            if operation == "mintToken" {
                return (200, json(#"{"access_token":"jwt","expires_in":900}"#))
            }
            return (401, nil)
        }
        let client = HubClient(
            hub: anyHub, tokens: MemoryCredentials(token: "refresh"), transport: transport
        )
        let flag = Flag()
        await client.notifyCredentialRejected { flag.raise() }

        await #expect(throws: HubClient.ClientError.self) {
            _ = try await client.sensors()
        }
        #expect(await log.count(of: "listSensors") == 2, "exactly one retry, never a loop")
        #expect(!flag.isRaised)
    }
}
```

- [ ] **Step 2: Run to verify the meaningful failures**

Expected: `staleTokenRetriesInvisibly` FAILS (today the first 401 throws — no retry). `revokedDeviceLandsFirstTry` FAILS (`listSensors` never reaches attempt semantics; the call throws without the handler firing — `flag.isRaised` is false). `persistent401StopsAfterOneRetry` FAILS on the count (1, not 2).

- [ ] **Step 3: Implement the retry in `BearerAuthMiddleware`**

Replace the struct with:

```swift
struct BearerAuthMiddleware: ClientMiddleware {
    let token: @Sendable () async throws -> String
    /// Asked only after a 401: drops the cached token and mints. Throwing
    /// here (the mint itself was rejected) is the revocation signal — the
    /// request is not resent.
    let freshToken: @Sendable () async throws -> String

    func intercept(
        _ request: HTTPRequest,
        body: HTTPBody?,
        baseURL: URL,
        operationID: String,
        next: (HTTPRequest, HTTPBody?, URL) async throws -> (HTTPResponse, HTTPBody?)
    ) async throws -> (HTTPResponse, HTTPBody?) {
        // The unauthenticated endpoints are the ones a client uses *before* it
        // has a credential. Asking for a token here would throw on the connect
        // screen, which is the one screen that must work without one.
        let unauthenticated: Set<String> = ["info", "pair", "mintToken", "pollPairing", "healthz"]
        guard !unauthenticated.contains(operationID) else {
            return try await next(request, body, baseURL)
        }

        // Buffered so the request can be sent twice: an HTTPBody is a stream
        // and may be single-shot. Every authenticated request this client
        // makes is a small JSON document; the cap matches StubTransport's.
        let payload: Data? = if let body {
            try await Data(collecting: body, upTo: 1 << 20)
        } else {
            nil
        }

        var request = request
        request.headerFields[.authorization] = "Bearer \(try await token())"
        let (response, responseBody) = try await next(
            request, payload.map { HTTPBody($0) }, baseURL
        )
        guard response.status == .unauthorized else { return (response, responseBody) }

        // One retry, through a mint that is forced to consult the hub. A
        // stale access token comes back replaced; a revoked device's mint
        // throws — which fires the rejection handler upstream — and the
        // request is not sent again.
        request.headerFields[.authorization] = "Bearer \(try await freshToken())"
        return try await next(request, payload.map { HTTPBody($0) }, baseURL)
    }
}
```

In `HubClient.init`, update the construction:

```swift
            middlewares: [
                BearerAuthMiddleware(
                    token: { try await provider.token() },
                    freshToken: { try await provider.freshToken() }
                )
            ]
```

- [ ] **Step 4: Run the full kit suite**

Expected: all pass — including `CredentialRejectionTests.staleAccessTokenStaysQuiet`, whose transport 401s every data call with a working mint: it now sees one retry then the throw, and its assertions (throws + handler quiet) hold unchanged.

- [ ] **Step 5: Commit and push the three iOS tasks**

```bash
git add BellasReefKit/Sources/BellasReefKit/BearerAuth.swift \
        BellasReefKit/Sources/BellasReefKit/HubClient.swift \
        BellasReefKit/Tests/BellasReefKitTests/RetryThroughMintTests.swift
git commit -m "fix(auth): one 401 is retried through a fresh mint, so a revoked device lands first try

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
```

---

### Task 4: The hub closes a revoked device's stream (backend)

Repo: `bellasreef`. `store.is_active` is checked once at the WS handshake and never again; a revoked device's Tank tab streams live telemetry until the socket happens to drop. Recheck in the send loop, time-gated. Chosen over a revoke→registry→disconnect push because one code path covers every revocation source (API endpoint, hub CLI, anything that touches the row), and a ≤10 s tail on a home hub is comfortably inside "good enough" — the push design would add a socket registry and a NATS hook to shave seconds off a window the client now closes itself anyway (Task 3 lands the device on the pairing screen at its next interaction regardless).

**Files:**
- Modify: `services/api/bellasreef_api/app.py` (stream endpoint ~line 1673; one module constant; one import)
- Test: `services/api/tests/test_stream_and_overrides.py` (extend `TestWebSocketStream`)

**Interfaces:**
- Consumes: `store.is_active(client_id: UUID) -> bool` (store.py:517), existing `queue`/`bridge` plumbing.
- Produces: module constant `STREAM_REVOKE_RECHECK_S: Final = 10.0` in `app.py` (monkeypatched by the test); close code `1008` reason `"client revoked"` — the same pair the handshake refusal uses, so a client sees one vocabulary.

- [ ] **Step 1: Write the failing test** (append inside `TestWebSocketStream`; follows the file's thread-portal idiom and its `# noqa: B017` precedent)

```python
    def test_a_revoked_client_is_disconnected_at_the_next_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revocation reaches an open socket, not just the next handshake.

        Found live 2026-08-13: a revoked device's Tank tab kept rendering
        telemetry because the only is_active check ran at the handshake.
        The recheck is time-gated at STREAM_REVOKE_RECHECK_S; zero here so
        the very next frame carries the check.
        """
        import bellasreef_api.app as app_module

        monkeypatch.setattr(app_module, "STREAM_REVOKE_RECHECK_S", 0.0)
        app, token, engine = self._app_and_token()

        async def revoke_self_and_publish() -> None:
            from bellasreef_contracts import ActuatorState, BinaryLevel
            from bellasreef_hardware_io.spine import Spine

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.delete(
                    "/api/v1/clients/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200, response.text

            spine = Spine(os.environ[_NATS])
            await spine.connect()
            await asyncio.sleep(0.4)  # let the bridge's subscription settle
            await spine.publish_state(
                ActuatorState(
                    message_id=uuid.uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="hardware-io",
                    actuator_id="led-blue",
                    level=BinaryLevel(on=False),
                    reason="commanded",
                    since=datetime.now(UTC),
                )
            )
            await spine.close()

        with TestClient(app) as client, client.websocket_connect("/api/v1/stream") as ws:
            ws.send_text(json.dumps({"token": token}))
            ready = json.loads(ws.receive_text())
            assert ready["kind"] == "ready"

            worker = threading.Thread(target=lambda: asyncio.run(revoke_self_and_publish()))
            worker.start()
            worker.join(timeout=30)

            # The frame that would have been sent instead carries the close.
            with pytest.raises(Exception):  # noqa: B017
                ws.receive_text()

        run(engine.dispose)
```

- [ ] **Step 2: Run to verify it fails**

Integration-gated: runs in CI, or locally only with dev containers. Locally without a container runtime it SKIPS — that is expected; note it and let CI be the red/green arbiter (Global Constraints). If dev containers are available: `cd services/api && uv run pytest tests/test_stream_and_overrides.py::TestWebSocketStream::test_a_revoked_client_is_disconnected_at_the_next_frame -v` — expected FAIL: the frame is delivered, `ws.receive_text()` returns instead of raising.

- [ ] **Step 3: Implement the recheck**

In `app.py`, next to the other module constants (top of file, near the imports — it must be a module global so tests can monkeypatch it):

```python
#: How stale an open stream's authorization may get. Checked in the send
#: loop, so a revoked device stops receiving within one frame or this many
#: seconds, whichever is later. A recheck is one indexed SELECT; at ~1 Hz
#: telemetry this is one extra query per client per ten seconds.
STREAM_REVOKE_RECHECK_S: Final = 10.0
```

Add `from time import monotonic` to the imports. Then in the stream endpoint, replace the send loop:

```python
        queue = await bridge.subscribe()
        last_authorized = monotonic()
        try:
            while True:
                frame = await queue.get()
                # Revocation must reach an open socket, not just the next
                # handshake — a revoked phone kept watching live telemetry
                # (2026-08-13). Same close code and reason as the handshake
                # refusal, so a client sees one vocabulary.
                if monotonic() - last_authorized > STREAM_REVOKE_RECHECK_S:
                    if not await store.is_active(client_id):
                        await websocket.close(code=1008, reason="client revoked")
                        return
                    last_authorized = monotonic()
                await websocket.send_text(frame)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bridge.unsubscribe(queue)
```

- [ ] **Step 4: Run the gate**

Run (repo root): `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh`
Expected: all local checks pass (the new test skips locally without containers; ruff/mypy must be clean).

- [ ] **Step 5: Commit, PR, CI, merge, deploy**

```bash
git checkout -b fix/stream-revocation-recheck
git add services/api/bellasreef_api/app.py services/api/tests/test_stream_and_overrides.py
git commit -m "fix(api): revocation reaches an open stream, not just the next handshake

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
BELLASREEF_ALLOW_ENV_SKIPS=1 git push -u origin fix/stream-revocation-recheck
gh pr create --fill   # then: gh pr checks --watch
```

CI green → merge (rebase) → `git checkout main && git pull --ff-only` → `./scripts/deploy-pi.sh` → confirm the script reports fresh telemetry on the wire. The integration test's real run is the CI leg — confirm the `lint · types · tests` job is green before merging, not just the build.

---

### Task 5: Live drill and closeout

Both repos. The unit is closed by the same test that opened it: revoke a device and watch it land, hands-off.

**Files:**
- Modify: `/Users/david/.claude/projects/-Users-david-visualstudio-bellasreef/memory/bellasreef-current-state.md` (strike the three backlog items)

**Interfaces:**
- Consumes: everything above, deployed (backend) and installed (sims).

- [ ] **Step 1: Build and install the fixed app on both sims**

```bash
cd /Users/david/visualstudio/bellasreef-ios
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
xcodebuild -project BellasReef.xcodeproj -scheme BellasReef \
  -destination 'platform=iOS Simulator,id=6D480152-FCE6-42CA-A56E-BA6D25167560' \
  -derivedDataPath /tmp/br-dd -skipPackagePluginValidation -skipMacroValidation build
for UDID in 6D480152-FCE6-42CA-A56E-BA6D25167560 9438872C-7EF2-4BA7-837F-1C55F938E6DF; do
  xcrun simctl terminate $UDID com.bellasreef.app 2>/dev/null
  xcrun simctl install $UDID /tmp/br-dd/Build/Products/Debug-iphonesimulator/BellasReef.app
  xcrun simctl launch $UDID com.bellasreef.app
done
```

(Both sims must be paired before the drill; if one is on "Find your hub", pair it first — code on the asking sim, System → Add a device → type code → Approve on the paired one.)

- [ ] **Step 2: The drill — David drives, hub watched**

On either sim: revoke the other device. On the revoked sim, touch nothing. Expected within ~10 s: its stream closes (hub side), the monitor reconnects, the forced mint is rejected, and the sim lands on "Find your hub" with the revocation notice — zero interactions. Verify the hub's story:

```bash
ssh bellasreef.local 'docker exec bellasreef-postgres-1 psql -U bellasreef -tA -c \
  "SELECT occurred_at, event->>'"'"'event'"'"' FROM audit_log ORDER BY occurred_at DESC LIMIT 5"'
```

Expected rows, newest first: `token.rejected` shortly after `client.revoked`. If the sim does not land, stop — that is a finding, not a flake; debug before closing.

- [ ] **Step 3: Re-pair the drilled sim** (leaves the bench at two live clients, the standing state)

- [ ] **Step 4: Close the books**

Strike the three backlog items from `bellasreef-current-state.md` (the "Backlog from revoke testing" bullet), replacing the bullet with one line: closed by this plan's commits, date-stamped. Commit nothing for this in the repos — it is memory, not code.

## Self-Review

- **Coverage:** item "two-interaction landing" → Tasks 2+3; "WS survives revoke" → Task 4; "double-mint" → Task 1; "close the unit out" → Task 5. No gaps.
- **Placeholders:** none — every step carries runnable code or an exact command.
- **Type consistency:** `mintFresh()`/`mintInFlight` named identically in Tasks 1–2; `freshToken`/`resolveFresh` consistent across Tasks 2–3; `STREAM_REVOKE_RECHECK_S` consistent in Task 4's test and implementation; close pair `1008`/`"client revoked"` matches the existing handshake refusal.
- **Risk noted:** Task 3 changes retry semantics for *every* authenticated call; the buffered-body cap (1 MiB) matches the test transport's and no current payload approaches it. Task 4's `queue.get()` frame gap means a fully idle stream is never rechecked — nothing is being sent to the revoked device in that case, and its next REST interaction lands it via Task 3.

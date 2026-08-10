# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""API service — auth and pairing surface (auth.md).

Stateless front door. This pass implements discovery, pairing, tokens and
client management only; the WebSocket stream and the sensor/override surface
land next.

`/info` and `/healthz` are the only unauthenticated endpoints, per auth.md §2.
"""

# NOTE: deliberately NOT `from __future__ import annotations`.
#
# The route handlers are closures inside build_app, and their
# `Annotated[UUID, Depends(current_client)]` parameters reference a function
# defined in that closure. With postponed annotations those become
# ForwardRefs that pydantic cannot resolve — it looks in the module
# namespace, where `current_client` does not exist — and OpenAPI generation
# fails with "is not fully defined".

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from uuid import UUID

from bellasreef_db import ClockUntrustedError, OverrideStore
from bellasreef_service import configure_logging, get_logger
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bellasreef_api.audit import NatsAuditSink
from bellasreef_api.frames import ReadyFrame
from bellasreef_api.security import (
    ACCESS_TOKEN_TTL_S,
    TokenError,
    issue_access_token,
    verify_access_token,
)
from bellasreef_api.store import PAIRING_TTL_S, Store
from bellasreef_api.stream import AUTH_TIMEOUT_S, StreamBridge, parse_auth_frame

__all__ = ["AuditSink", "build_app"]

log = get_logger(__name__)

SERVICE: Final = "api"
API_VERSION: Final = "v1"
CONTRACTS_VERSION: Final = "2.0.0"

#: Every auth event goes here, per auth.md §3 and the existing audit contract.
AuditSink = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop_audit(event: str, detail: dict[str, Any]) -> None:
    log.warning("auth event not audited: no sink configured", extra={"event": event})


# --------------------------------------------------------------------- schemas


class Info(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    api_version: str
    contracts_version: str
    paired_client_count: int
    pairing_open: bool


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: str = Field(min_length=1, max_length=128)


class PairGranted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str
    client_id: UUID


class PairPending(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    poll_after_s: int
    expires_in_s: int


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str


class AccessToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class Client(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=64)
    duty: float = Field(ge=0.0, le=1.0)
    duration_s: float = Field(gt=0.0, le=86400.0)
    reason: str | None = Field(default=None, max_length=256)


class OverrideView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    target: str
    duty: float
    expires_at: datetime
    expires_in_s: float


@dataclass
class _PendingTokens:
    """Approved-but-uncollected refresh tokens, held in memory only.

    The token is minted when a paired client approves, but delivered when the
    new client next polls. It is deliberately **not** persisted: writing a
    plaintext refresh token to Postgres would defeat storing only its hash, and
    the whole point of the hash is that a database dump is worthless.

    The cost is that an API restart between approval and collection loses the
    token. That is acceptable — pairing is a once-ever flow, the poller gets a
    clean 404 and starts again, and the failure is annoying rather than unsafe.
    """

    tokens: dict[UUID, tuple[UUID, str]]

    def put(self, request_id: UUID, client_id: UUID, token: str) -> None:
        self.tokens[request_id] = (client_id, token)

    def take(self, request_id: UUID) -> tuple[UUID, str] | None:
        return self.tokens.pop(request_id, None)


# ------------------------------------------------------------------------ app


def build_app(
    engine: AsyncEngine,
    *,
    audit: AuditSink | None = None,
    access_ttl_s: int = ACCESS_TOKEN_TTL_S,
    nats_url: str | None = None,
    clock_trusted: Callable[[], bool] | None = None,
) -> FastAPI:
    store = Store(engine)
    overrides = OverrideStore(engine, clock_trusted=clock_trusted)
    bridge = StreamBridge(nats_url, overrides) if nats_url else None
    sink: AuditSink = audit or _noop_audit
    pending = _PendingTokens(tokens={})

    # Resolved on first use and cached, rather than in a lifespan hook. The
    # key is created on first boot and then never changes, so eager loading
    # buys nothing — and making it lazy means the app works identically under
    # uvicorn, under an ASGI test transport that never runs lifespan, and in a
    # second worker process that starts after the key already exists.
    secret_cache: dict[str, str] = {}

    async def signing_secret() -> str:
        if "secret" not in secret_cache:
            secret_cache["secret"] = await store.signing_secret()
        return secret_cache["secret"]

    app = FastAPI(
        title="Bella's Reef API",
        version=CONTRACTS_VERSION,
        summary="Reef controller: pairing, auth, and device control.",
    )
    app.state.signing_secret_getter = signing_secret

    async def current_client(
        authorization: Annotated[str | None, Header()] = None,
    ) -> UUID:
        """Resolve the caller, or 401.

        Checks the signature *and* that the client is still active. A JWT alone
        is not enough: revocation stops new tokens being minted but cannot
        recall outstanding ones, so the liveness check is what makes revocation
        take effect inside the token's remaining lifetime for stateful
        operations.
        """
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        try:
            client_id = verify_access_token(authorization[7:], await signing_secret())
        except TokenError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        if not await store.is_active(client_id):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "client revoked")
        return client_id

    # ---------------------------------------------------------- unauthenticated

    @app.get("/healthz", tags=["ops"], operation_id="health")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/info", response_model=Info, tags=["discovery"], operation_id="info")
    async def info() -> Info:
        """Renders the connect screen before any commitment.

        Unauthenticated by design, and returns nothing sensitive: a name, two
        version strings, and whether pairing is open.
        """
        total = await store.total_clients_ever()
        return Info(
            name="Bella's Reef",
            api_version=API_VERSION,
            contracts_version=CONTRACTS_VERSION,
            paired_client_count=total,
            pairing_open=total == 0,
        )

    # ------------------------------------------------------------------ pairing

    @app.post(
        "/api/v1/pair",
        tags=["pairing"],
        status_code=status.HTTP_200_OK,
        operation_id="pair",
    )
    async def pair(body: PairRequest, response: Response) -> PairGranted | PairPending:
        """Pair a client. Four outcomes, in this order.

        1. No client has ever paired -> TOFU grant, and the window shuts.
        2. A recovery window is open -> grant and spend it.
        3. Someone is alive to approve -> 202, poll for a decision.
        4. Clients exist but every one is revoked -> 403. There is nobody to
           approve, so the honest answer is to tell the operator to run
           `bellasreef pair` on the hub rather than leave them polling a
           request no one will ever see.
        """
        total = await store.total_clients_ever()

        if total == 0:
            client_id, token = await store.create_client(body.client_name)
            await sink(
                "pair.tofu_granted",
                {"client_id": str(client_id), "client_name": body.client_name},
            )
            log.warning(
                "TOFU window used and now closed",
                extra={"client_id": str(client_id), "event": "pair_tofu"},
            )
            return PairGranted(refresh_token=token, client_id=client_id)

        window_id = await store.open_window()
        if window_id is not None:
            client_id, token = await store.create_client(body.client_name)
            await store.consume_window(window_id, client_id)
            await sink(
                "pair.window_used",
                {
                    "window_id": str(window_id),
                    "client_id": str(client_id),
                    "client_name": body.client_name,
                },
            )
            log.warning(
                "recovery pairing window used and now spent",
                extra={"window_id": str(window_id), "client_id": str(client_id)},
            )
            return PairGranted(refresh_token=token, client_id=client_id)

        if await store.active_client_count() == 0:
            await sink("pair.no_approver", {"client_name": body.client_name})
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "every paired client has been revoked, so there is nobody to "
                "approve this request. Run `bellasreef pair` on the hub to open a "
                "recovery window.",
            )

        request_id = await store.open_pairing_request(body.client_name)
        await sink(
            "pair.requested",
            {"request_id": str(request_id), "client_name": body.client_name},
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return PairPending(request_id=request_id, poll_after_s=5, expires_in_s=PAIRING_TTL_S)

    @app.get("/api/v1/pair/{request_id}", tags=["pairing"], operation_id="pollPairing")
    async def poll_pair(request_id: UUID, response: Response) -> PairGranted | dict[str, str]:
        state, _, _ = await store.pairing_state(request_id)

        if state == "missing":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such pairing request")
        if state == "expired":
            raise HTTPException(status.HTTP_410_GONE, "pairing request expired")
        if state == "denied":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "pairing denied")
        if state == "pending":
            response.status_code = status.HTTP_202_ACCEPTED
            return {"status": "pending"}

        collected = pending.take(request_id)
        if collected is None:
            # Approved, but the token was already collected or the API
            # restarted before collection. Either way it cannot be re-issued —
            # re-issuing on demand would turn one approval into unlimited
            # credentials.
            raise HTTPException(status.HTTP_410_GONE, "approval already collected")

        client_id, token = collected
        await sink("pair.collected", {"client_id": str(client_id)})
        return PairGranted(refresh_token=token, client_id=client_id)

    @app.post(
        "/api/v1/pair/{request_id}/approve",
        tags=["pairing"],
        operation_id="approvePairing",
    )
    async def approve(
        request_id: UUID, approver: Annotated[UUID, Depends(current_client)]
    ) -> dict[str, str]:
        """Approve from an already-paired client. The trust anchor is the tap."""
        result = await store.approve_pairing(request_id)
        if result is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "pairing request is not pending")
        client_id, token = result
        pending.put(request_id, client_id, token)
        await sink(
            "pair.approved",
            {
                "request_id": str(request_id),
                "client_id": str(client_id),
                "approved_by": str(approver),
            },
        )
        return {"status": "approved"}

    @app.post("/api/v1/pair/{request_id}/deny", tags=["pairing"], operation_id="denyPairing")
    async def deny(
        request_id: UUID, approver: Annotated[UUID, Depends(current_client)]
    ) -> dict[str, str]:
        if not await store.deny_pairing(request_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "pairing request is not pending")
        await sink("pair.denied", {"request_id": str(request_id), "denied_by": str(approver)})
        return {"status": "denied"}

    # ------------------------------------------------------------------- tokens

    @app.post(
        "/api/v1/token",
        response_model=AccessToken,
        tags=["auth"],
        operation_id="mintToken",
    )
    async def token(body: TokenRequest) -> AccessToken:
        client_id = await store.client_for_refresh_token(body.refresh_token)
        if client_id is None:
            # Covers unknown and revoked identically: a revoked row has a NULL
            # hash and cannot match, so there is no separate check to forget.
            await sink("token.rejected", {"reason": "unknown or revoked refresh token"})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

        access, expires_in = issue_access_token(
            client_id, await signing_secret(), ttl_s=access_ttl_s
        )
        await sink("token.minted", {"client_id": str(client_id)})
        return AccessToken(access_token=access, expires_in=expires_in)

    # ------------------------------------------------------------------ clients

    @app.get(
        "/api/v1/clients",
        response_model=list[Client],
        tags=["clients"],
        operation_id="listClients",
    )
    async def list_clients(_: Annotated[UUID, Depends(current_client)]) -> list[Client]:
        return [
            Client(
                id=row.id,
                name=row.name,
                created_at=row.created_at,
                last_seen_at=row.last_seen_at,
                revoked_at=row.revoked_at,
            )
            for row in await store.list_clients()
        ]

    @app.delete("/api/v1/clients/{client_id}", tags=["clients"], operation_id="revokeClient")
    async def revoke_client(
        client_id: UUID, actor: Annotated[UUID, Depends(current_client)]
    ) -> dict[str, str]:
        """Revoke. Any paired client may revoke any other, including itself.

        auth.md §1: revocation is the only privilege operation and a paired
        device has full operator rights. A lost phone is revoked from the one
        you still have.
        """
        if not await store.revoke(client_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or already revoked")
        await sink("client.revoked", {"client_id": str(client_id), "revoked_by": str(actor)})
        return {"status": "revoked"}

    # ----------------------------------------------------------- hardware

    @app.get("/api/v1/devices", tags=["hardware"], operation_id="listDevices")
    async def devices(_: Annotated[UUID, Depends(current_client)]) -> list[dict[str, Any]]:
        """Registered hardware. *Devices* are the tank's; clients are people's."""
        return await store.list_devices()

    @app.get("/api/v1/sensors", tags=["hardware"], operation_id="listSensors")
    async def sensors(_: Annotated[UUID, Depends(current_client)]) -> list[dict[str, Any]]:
        return await store.list_devices(kind="sensor")

    # ---------------------------------------------------------- overrides

    @app.get(
        "/api/v1/overrides",
        response_model=list[OverrideView],
        tags=["overrides"],
        operation_id="listOverrides",
    )
    async def list_overrides(
        _: Annotated[UUID, Depends(current_client)],
    ) -> list[OverrideView]:
        now = datetime.now(UTC)
        return [
            OverrideView(
                id=o.id,
                target=o.target,
                duty=o.duty,
                expires_at=o.expires_at,
                expires_in_s=max(0.0, round((o.expires_at - now).total_seconds(), 1)),
            )
            for o in await overrides.list_active()
        ]

    @app.post(
        "/api/v1/overrides",
        response_model=OverrideView,
        tags=["overrides"],
        operation_id="createOverride",
    )
    async def create_override(
        body: OverrideRequest, actor: Annotated[UUID, Depends(current_client)]
    ) -> OverrideView:
        """Hold a target at a level for a duration.

        Clock-gated: an override IS a deadline, and one computed from a clock
        chrony is about to step is not the duration the operator asked for.
        """
        try:
            placed = await overrides.create(
                body.target, body.duty, body.duration_s, reason=body.reason
            )
        except ClockUntrustedError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        await sink(
            "override.created",
            {
                "override_id": str(placed.id),
                "target": placed.target,
                "duty": placed.duty,
                "expires_at": placed.expires_at.isoformat(),
                "actor": str(actor),
            },
        )
        return OverrideView(
            id=placed.id,
            target=placed.target,
            duty=placed.duty,
            expires_at=placed.expires_at,
            expires_in_s=round(body.duration_s, 1),
        )

    @app.delete(
        "/api/v1/overrides/{override_id}",
        tags=["overrides"],
        operation_id="releaseOverride",
    )
    async def release_override(
        override_id: UUID, actor: Annotated[UUID, Depends(current_client)]
    ) -> dict[str, str]:
        if not await overrides.release(override_id, "manual"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or already released")
        await sink(
            "override.released",
            {"override_id": str(override_id), "actor": str(actor)},
        )
        return {"status": "released"}

    # ------------------------------------------------------------- stream

    @app.websocket("/api/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        """Live state and sensor fan-out.

        Authenticated by the FIRST MESSAGE, not a header or query parameter:
        browsers cannot set headers on a WebSocket handshake, and a token in a
        URL ends up in access logs and proxy history.
        """
        await websocket.accept()

        if bridge is None:
            await websocket.close(code=1011, reason="stream unavailable: no spine")
            return

        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_S)
        except (TimeoutError, WebSocketDisconnect):
            await websocket.close(code=1008, reason="authentication timed out")
            return

        token = parse_auth_frame(raw)
        if token is None:
            await websocket.close(code=1008, reason='first message must be {"token": ...}')
            return
        try:
            client_id = verify_access_token(token, await signing_secret())
        except TokenError:
            await websocket.close(code=1008, reason="invalid token")
            return
        if not await store.is_active(client_id):
            await websocket.close(code=1008, reason="client revoked")
            return

        await websocket.send_text(
            ReadyFrame(received_at=datetime.now(UTC), client_id=client_id).model_dump_json()
        )
        queue = await bridge.subscribe()
        try:
            while True:
                await websocket.send_text(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bridge.unsubscribe(queue)

    return app


def create_app() -> FastAPI:
    configure_logging(service=SERVICE, level=os.environ.get("BELLASREEF_LOG_LEVEL", "INFO"))
    dsn = os.environ["BELLASREEF_DATABASE_URL"]

    nats_url = os.environ.get("BELLASREEF_NATS_URL")
    if nats_url:
        sink: AuditSink | None = NatsAuditSink(nats_url)
    else:
        # Loud, because auth.md §3 requires these events on the trail and the
        # no-op only logs them locally.
        log.critical(
            "BELLASREEF_NATS_URL is unset: auth events will NOT reach bellasreef.audit.auth"
        )
        sink = None

    return build_app(create_async_engine(dsn, future=True), audit=sink, nats_url=nats_url)

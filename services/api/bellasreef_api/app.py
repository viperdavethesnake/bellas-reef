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
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from bellasreef_db import AlertRecord, ClockUntrustedError, OverrideStore, PostgresAlertStore
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
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bellasreef_api.audit import NatsAuditSink
from bellasreef_api.audit_writer import AuditWriter
from bellasreef_api.frames import ReadyFrame
from bellasreef_api.history import DEFAULT_BUCKETS, MAX_BUCKETS, HistoryReader
from bellasreef_api.registry import CapabilityConsumer, RegistryConsumer
from bellasreef_api.security import (
    ACCESS_TOKEN_TTL_S,
    TokenError,
    issue_access_token,
    verify_access_token,
)
from bellasreef_api.store import PAIRING_TTL_S, Store
from bellasreef_api.stream import AUTH_TIMEOUT_S, StreamBridge, parse_auth_frame
from bellasreef_api.telemetry import TelemetryWriter

__all__ = ["AuditSink", "build_app"]

log = get_logger(__name__)

SERVICE: Final = "api"
API_VERSION: Final = "v1"
#: Derived, never written out. This was a string literal, contracts went to
#: 3.1.0 for the silence class, and the literal stayed at 3.0.0 — so `/info`
#: told every client it spoke an older contract than it did, and the same stale
#: string went into every backup manifest. A hand-maintained copy of a number
#: that lives somewhere else has exactly one behaviour over time.
CONTRACTS_VERSION: Final = version("bellasreef-contracts")

#: Every authenticated route can return 401 via the current_client
#: dependency. Declared and shared so a client can MODEL "your credential
#: stopped working" rather than pattern-matching an undocumented status.
AUTH_401: Final[dict[str, str]] = {
    "description": "Missing, invalid, expired, or revoked credential."
}

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
    #: True when at least one live client exists to approve a new one.
    #:
    #: Distinct from ``pairing_open``, and the distinction is the whole point.
    #: ``pairing_open`` is keyed on clients *ever* paired, so that revoking
    #: everything cannot reopen trust-on-first-use. That leaves a third state
    #: the old contract could not express: paired before, nothing live now,
    #: nobody able to approve anyone. A client that cannot see this renders
    #: "an already-paired device will need to approve this one" at a person who
    #: has no such device, and they wait for something that can never arrive.
    approvers_available: bool


class HistoryBucket(BaseModel):
    """One downsampled interval.

    ``average`` is the line; ``minimum``/``maximum`` are the envelope. Clients
    must draw the band — a bucket whose spike is only in ``maximum`` is exactly
    the sample an alert episode was raised on.
    """

    model_config = ConfigDict(extra="forbid")

    at: datetime
    minimum: float
    average: float
    maximum: float


class HistorySeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    metric: str
    unit: str
    #: Buckets with data. Absent buckets are gaps and must be rendered as
    #: breaks: `bellasreef_actuator_level` comes from a last-value-retained
    #: stream, so duty genuinely has holes, and a line drawn across one asserts
    #: a continuity nothing measured.
    buckets: list[HistoryBucket]


class HistoryEpisode(BaseModel):
    """An alert episode, for banding under the curve."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    sensor_type: str

    #: ``threshold`` or ``silence``. The client draws these differently rather
    #: than inferring from null fields: a band saying "we stopped knowing" is a
    #: different statement from one saying "it was too cold", and rendering them
    #: alike would let the more serious one hide inside the other.
    alert_class: Literal["threshold", "silence"]

    bound: Literal["min", "max"] | None = None
    threshold: float | None = None
    unit: str | None = None
    raised_at: datetime
    raised_value: float | None = None

    #: Silence only. When the probe was last heard from, which is earlier than
    #: ``raised_at`` by the deadline that had to elapse first — so a client can
    #: start the band where the data actually stopped rather than where the hub
    #: got around to noticing.
    last_reading_at: datetime | None = None

    #: ``None`` while the episode is still open. Left open deliberately; the
    #: client clamps the band to the window edge rather than the hub inventing
    #: a clear time that never happened.
    cleared_at: datetime | None = None
    cleared_value: float | None = None


class HistoryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    bucket_s: int
    series: list[HistorySeries]
    episodes: list[HistoryEpisode]


class AuditEvent(BaseModel):
    """One persisted audit event."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    occurred_at: datetime
    category: str
    actor: str
    subject: str
    device_id: str | None
    event: dict[str, Any]


class CapabilityView(BaseModel):
    """One thing this hub's hardware can offer, and whether it is claimed.

    The app's "find devices" source. A capability is a fact about the hardware —
    this hub has PWM channels — true whether or not anybody has decided what
    they are for. `bound_to` is the device that claimed it, or null if it is
    free to bind.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["pi-pwm", "pca9685", "w1-bus"]
    #: Stable within its source, and what a binding names: the channel number
    #: for PWM, the ROM code for a 1-Wire probe.
    channel: str
    #: Whatever the announcement carried that is worth rendering — the GPIO a
    #: channel reaches, the I2C address, the bus master.
    detail: dict[str, Any]
    announced_at: datetime
    #: The device_id that has claimed this channel, or null.
    bound_to: str | None = None

    @property
    def bound(self) -> bool:
        return self.bound_to is not None


#: Which announced source a driver type binds against. A DS18B20 is a probe on
#: the w1-bus rather than a source of its own, so the two names differ and the
#: mapping has to be explicit.
CAPABILITY_SOURCE_FOR_DRIVER: Final[dict[str, str]] = {
    "pi-pwm": "pi-pwm",
    "pca9685": "pca9685",
    "ds18b20": "w1-bus",
}


class BindDeviceRequest(BaseModel):
    """Declare a device by binding it to an announced capability channel."""

    model_config = ConfigDict(extra="forbid")

    #: Proposed id. Ignored if the capability is already carried by an existing
    #: device — that device IS this hardware, and a caller proposing a new id
    #: for known hardware is renaming at most, never creating.
    device_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    driver_type: Literal["pi-pwm", "pca9685", "ds18b20"]
    #: The capability channel: the channel number for PWM, the ROM for 1-Wire.
    channel: str = Field(min_length=1, max_length=64)
    #: Only ``light`` is implemented. Actuators require it; sensors must not
    #: carry one.
    role: Literal["light"] | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    location: str | None = Field(default=None, min_length=1, max_length=128)
    poll_interval_s: float | None = Field(default=None, gt=0)


class BoundDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    created: bool
    driver_type: str
    channel: str


class DeviceView(BaseModel):
    """A registered device, as clients render it.

    Typed rather than a bare dict. The endpoints used to return
    ``list[dict[str, Any]]``, which generates as an opaque object container in
    Swift and forces the client to reach in by string key — precisely the
    hand-written binding the generated-client rule exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str
    #: ``None`` means the operator has not named it; clients fall back to
    #: ``device_id``. Never defaulted server-side — see migration 0007.
    display_name: str | None
    kind: str
    driver_id: str
    enabled: bool

    sensor_type: str | None = None
    poll_interval_s: float | None = None
    actuator_class: str | None = None
    role: str | None = None
    #: Whether a command to this device is a guarantee or a hope
    #: (docs/device-classes.md §2). Clients must not render an advisory value
    #: with the same weight as a measured one — §5.
    control_authority: str | None = None
    failsafe_capable: bool | None = None
    transport: str | None = None
    #: The safety contract, surfaced rather than hidden. A client showing an
    #: actuator should be able to say what it does when everything fails.
    safe_state: dict[str, Any] | None = None
    max_runtime_s: float | None = None
    heartbeat_timeout_s: float | None = None

    alert_min: float | None = None
    alert_max: float | None = None
    alert_clear_margin: float | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.device_id


class DeviceName(BaseModel):
    """A new display name, or ``null`` to go back to the raw id."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _blank_is_not_a_name(self) -> "DeviceName":
        """Whitespace is not a name.

        Without this, "   " stores as a non-NULL value and every client shows a
        blank label where the id used to be, with no way back short of another
        request. Normalising it to NULL means clearing the field and never
        setting one are the same state, which is the honest model.
        """
        if self.display_name is not None:
            trimmed = self.display_name.strip()
            object.__setattr__(self, "display_name", trimmed or None)
        return self


class DeviceNameView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    display_name: str | None


class AlertThresholds(BaseModel):
    """The band a sensor is expected to stay inside (PRD R12).

    All three are nullable together: clearing every field turns alerting off for
    the device, which is the only way to say "stop watching this" without a
    separate verb.
    """

    model_config = ConfigDict(extra="forbid")

    minimum: float | None = None
    maximum: float | None = None
    #: How far back inside the band a reading must come before a breach clears.
    clear_margin: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _mirror_the_database_constraints(self) -> "AlertThresholds":
        """Reject here what Postgres would reject anyway.

        Not redundant: without this the operator gets a 500 naming a constraint,
        and with it a 422 naming a field. The database remains the authority —
        this is the error message, not the enforcement.
        """
        if self.minimum is None and self.maximum is None:
            return self
        if self.clear_margin is None:
            raise ValueError("clear_margin is required when a threshold is set")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum >= self.maximum:
                raise ValueError("minimum must be below maximum")
            if self.minimum + self.clear_margin >= self.maximum - self.clear_margin:
                raise ValueError(
                    "clear_margin is wider than half the band, so no reading could clear it"
                )
        return self


class AlertThresholdsView(AlertThresholds):
    """Thresholds as stored, with the device they belong to."""

    device_id: str


class AlertView(BaseModel):
    """One alert episode."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    device_id: str
    sensor_type: str

    #: ``threshold`` or ``silence``. Everything below the fold is threshold-only
    #: and null on a silence, which has no bound, no band and no raising
    #: reading. Clients switch on this rather than sniffing for nulls: a probe
    #: that has gone quiet is a different thing to draw, not a breach with
    #: missing fields.
    alert_class: Literal["threshold", "silence"]

    bound: Literal["min", "max"] | None = None
    threshold: float | None = None
    clear_margin: float | None = None
    unit: str | None = None
    raised_at: datetime
    raised_value: float | None = None

    #: Silence only: when the probe was last heard from, which is earlier than
    #: ``raised_at`` by the deadline that had to elapse before anyone noticed.
    last_reading_at: datetime | None = None

    cleared_at: datetime | None = None
    cleared_value: float | None = None

    @property
    def active(self) -> bool:
        return self.cleared_at is None


class AlertsView(BaseModel):
    """What is wrong now, and what has been wrong lately.

    Both in one response because a client showing an alerts screen needs both,
    and two round trips would let them disagree — an episode can clear between
    the first call and the second, and the UI would show it as simultaneously
    active and resolved.
    """

    model_config = ConfigDict(extra="forbid")

    active: list[AlertView]
    recent: list[AlertView]


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


def _alert_view(row: AlertRecord) -> "AlertView":
    """Widen the store's stringly-typed columns to the literals the API promises.

    The database CHECKs already guarantee both — `alert_class` is
    threshold-or-silence, and `bound` is min-or-max whenever the class is
    threshold. This is where the type system is told, once, rather than at every
    call site.
    """
    alert_class: Literal["threshold", "silence"] = (
        "silence" if row.alert_class == "silence" else "threshold"
    )
    bound: Literal["min", "max"] | None = None
    if row.bound is not None:
        bound = "min" if row.bound == "min" else "max"
    return AlertView(
        id=row.id,
        device_id=row.device_id,
        sensor_type=row.sensor_type,
        alert_class=alert_class,
        bound=bound,
        threshold=row.threshold,
        clear_margin=row.clear_margin,
        unit=row.unit,
        raised_at=row.raised_at,
        raised_value=row.raised_value,
        last_reading_at=row.last_reading_at,
        cleared_at=row.cleared_at,
        cleared_value=row.cleared_value,
    )


def build_app(
    engine: AsyncEngine,
    *,
    audit: AuditSink | None = None,
    access_ttl_s: int = ACCESS_TOKEN_TTL_S,
    nats_url: str | None = None,
    vm_url: str | None = None,
    clock_trusted: Callable[[], bool] | None = None,
    durable_suffix: str = "",
) -> FastAPI:
    store = Store(engine)
    overrides = OverrideStore(engine, clock_trusted=clock_trusted)
    alerts = PostgresAlertStore(engine)
    reader = HistoryReader(vm_url) if vm_url else None
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

    registry = RegistryConsumer(nats_url, store) if nats_url else None
    capabilities = CapabilityConsumer(nats_url, store) if nats_url else None
    telemetry = (
        TelemetryWriter(nats_url, vm_url, store, durable_suffix=durable_suffix)
        if nats_url and vm_url
        else None
    )
    audit_writer = (
        AuditWriter(nats_url, engine, durable=f"audit-writer{durable_suffix}") if nats_url else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Run the registry consumer for the life of the process.

        A lifespan rather than lazy-on-first-use, because this one has to be
        listening whether or not anybody has made a request yet — that is the
        entire point of it. Note that the ASGI transport used in tests does not
        run lifespans, so the consumer stays out of the way there.
        """
        # Mint the hub's identity if this is its first boot. Here rather than
        # in the migration, which also runs on a hub about to have a backup
        # restored into it — stamping an id there would give the restored
        # database a new identity and destroy the fact the row exists to carry.
        #
        # Best effort: a hub that cannot write its own name should still serve
        # temperatures. It shows up as a backup manifest with a null hub_id,
        # which the archive format already treats as "old, not wrong".
        try:
            await store.hub_id()
        except Exception:
            log.exception("could not establish hub identity")

        for name, component in (
            ("registry consumer", registry),
            ("capability consumer", capabilities),
            ("telemetry writer", telemetry),
            ("audit writer", audit_writer),
        ):
            if component is None:
                continue
            try:
                await component.start()
            except Exception:  # broad by design: no spine must not stop the API
                log.exception("%s failed to start", name)
        try:
            yield
        finally:
            for component in (registry, telemetry, audit_writer):
                if component is not None:
                    await component.close()

    app = FastAPI(
        title="Bella's Reef API",
        version=CONTRACTS_VERSION,
        summary="Reef controller: pairing, auth, and device control.",
        lifespan=lifespan,
    )
    app.state.signing_secret_getter = signing_secret
    # Named so a test can assert these are *running*, not merely constructed.
    # The audit writer spent its whole life constructible and unstarted.
    app.state.background = {
        "registry consumer": registry,
        "capability consumer": capabilities,
        "telemetry writer": telemetry,
        "audit writer": audit_writer,
    }

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

    @app.get(
        "/api/v1/capabilities",
        response_model=list[CapabilityView],
        tags=["devices"],
        operation_id="listCapabilities",
        responses={401: AUTH_401},
    )
    async def list_capabilities(
        _: Annotated[UUID, Depends(current_client)],
    ) -> list[CapabilityView]:
        """What the hardware can offer, and what has been claimed.

        Tier one of the registry, and the app's find-devices source. Announced
        by hardware-io on startup; nothing here is a device until an operator
        binds it.
        """
        return [CapabilityView(**row) for row in await store.list_capabilities()]

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
            approvers_available=await store.active_client_count() > 0,
        )

    # ------------------------------------------------------------------ pairing

    @app.post(
        "/api/v1/pair",
        tags=["pairing"],
        status_code=status.HTTP_200_OK,
        operation_id="pair",
        response_model=None,
        # Declared so generated clients can MODEL these outcomes rather than
        # inspecting raw status codes. An undeclared response forces every
        # client to hand-roll the branch — the drift G3 exists to prevent.
        responses={
            200: {"model": PairGranted, "description": "Paired; credential issued."},
            202: {"model": PairPending, "description": "Awaiting approval; poll."},
            403: {"description": "Nobody can approve; run `bellasreef pair`."},
        },
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

    @app.get(
        "/api/v1/pair/{request_id}",
        tags=["pairing"],
        operation_id="pollPairing",
        response_model=None,
        responses={
            200: {"model": PairGranted, "description": "Approved; credential issued."},
            202: {"description": "Still pending."},
            403: {"description": "Denied."},
            404: {"description": "No such request."},
            410: {"description": "Expired, or the credential was already collected."},
        },
    )
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
        responses={
            401: AUTH_401,
            409: {"description": "The pairing request is not pending."},
        },
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

    @app.post(
        "/api/v1/pair/{request_id}/deny",
        tags=["pairing"],
        operation_id="denyPairing",
        responses={
            401: AUTH_401,
            409: {"description": "The pairing request is not pending."},
        },
    )
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
        responses={401: {"description": "Unknown or revoked refresh token."}},
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
        responses={401: AUTH_401},
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

    # Declared BEFORE /clients/{client_id}. FastAPI matches routes in order, and
    # "me" would otherwise be parsed as a UUID path parameter and 422 before it
    # ever reached a handler.
    @app.delete(
        "/api/v1/clients/me",
        tags=["clients"],
        operation_id="revokeSelf",
        responses={401: AUTH_401},
    )
    async def revoke_self(actor: Annotated[UUID, Depends(current_client)]) -> dict[str, str]:
        """Revoke the calling client.

        Signing out has to reach the hub. Forgetting the credential locally
        leaves a row the hub still counts as live, so the hub believes someone
        can approve a new device while the only device that could is the one
        that just signed out — a lockout with no way back except the recovery
        CLI.
        """
        await store.revoke(actor)
        await sink("client.revoked", {"client_id": str(actor), "actor": str(actor), "self": True})
        return {"status": "revoked"}

    @app.delete(
        "/api/v1/clients/{client_id}",
        tags=["clients"],
        operation_id="revokeClient",
        responses={
            401: AUTH_401,
            404: {"description": "Unknown or already revoked."},
        },
    )
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

    @app.post(
        "/api/v1/devices",
        response_model=BoundDevice,
        tags=["hardware"],
        operation_id="bindDevice",
        responses={
            401: AUTH_401,
            404: {"description": "No such capability channel has been announced."},
            409: {"description": "That channel is already bound to another device."},
            422: {"description": "The role is not legal for this device."},
        },
    )
    async def bind_device(
        body: BindDeviceRequest,
        _: Annotated[UUID, Depends(current_client)],
    ) -> BoundDevice:
        """Bind an announced capability channel to a device.

        Three validations, in the order that gives the most useful answer.

        **The channel must have been announced.** Binding to hardware nobody has
        reported means a device that can never work, and the operator finds out
        when the tank does not light rather than here.

        **It must not already be bound.** Two devices on one channel do not
        coexist, they interleave — the second write wins, intermittently.

        **The role must be legal.** ``light`` is the only one implemented; the
        contract's other roles are reserved, and accepting one would register a
        device nothing knows how to drive.

        Then it **matches before it creates.** If a device already carries this
        binding, that device is this hardware and is adopted in place, whatever
        id was proposed. A seed that created beside an existing probe forked a
        tank's history in two, and this is the check that makes that
        unrepresentable.
        """
        is_sensor = body.driver_type == "ds18b20"

        # The driver type is not the capability source. A DS18B20 is a probe on
        # the w1-bus; the bus is what hardware-io announces, and looking up
        # source='ds18b20' would 404 every probe on a hub that is working
        # perfectly.
        source = CAPABILITY_SOURCE_FOR_DRIVER[body.driver_type]

        if not await store.capability_exists(source, body.channel):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no {source} capability announced for channel {body.channel!r}. "
                "hardware-io announces what it can see at startup; a channel that is not "
                "there cannot be bound.",
            )

        # The double-bind check applies to actuators only, and the asymmetry is
        # the point.
        #
        # A probe has its own identity: the ROM is burned in, so a request
        # naming that ROM is *this* hardware whatever id it proposes, and the
        # right answer is to adopt the device already holding it. Refusing here
        # is what produced the identity fork — a seed proposing a new name for a
        # known probe got a row of its own.
        #
        # A PWM channel has no such identity. It is a slot, and the only thing
        # that says what is plugged into it is the operator's declaration. So a
        # second declaration on a taken channel is a mistake to report, not
        # hardware to recognise.
        holder = await store.device_bound_to(body.driver_type, body.channel)
        if not is_sensor and holder is not None and holder != body.device_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{body.driver_type} channel {body.channel!r} is already bound to "
                f"{holder!r}. Unbind it first — two devices on one channel interleave "
                "rather than coexist.",
            )

        # Cadence is required for a sensor rather than defaulted. It sets the
        # silence deadline (6x cadence), so a silent default would pick the
        # threshold at which the hub declares a probe dead — a number nobody
        # chose. The devices CHECK requires it too.
        if is_sensor and body.poll_interval_s is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "a sensor must declare poll_interval_s: it sets the deadline at which "
                "the probe is reported silent, and defaulting it would pick that "
                "threshold on the operator's behalf",
            )

        if is_sensor and body.role is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "a sensor carries no role; sensor_type already says what it is",
            )
        if not is_sensor and body.role is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "an actuator must declare a role. Only 'light' is implemented.",
            )

        binding = {"rom": body.channel} if is_sensor else {"channel": body.channel}
        device_id, created = await store.bind_device(
            device_id=body.device_id,
            kind="sensor" if is_sensor else "actuator",
            driver_type=body.driver_type,
            channel=body.channel,
            binding=binding,
            role=body.role,
            display_name=body.display_name,
            location=body.location,
            sensor_type="temp" if is_sensor else None,
            poll_interval_s=body.poll_interval_s if is_sensor else None,
        )

        await sink(
            "device.bound",
            {
                "device_id": device_id,
                "driver_type": body.driver_type,
                "channel": body.channel,
                "created": created,
            },
        )
        return BoundDevice(
            device_id=device_id,
            created=created,
            driver_type=body.driver_type,
            channel=body.channel,
        )

    @app.get(
        "/api/v1/devices",
        response_model=list[DeviceView],
        tags=["hardware"],
        operation_id="listDevices",
        responses={401: AUTH_401},
    )
    async def devices(_: Annotated[UUID, Depends(current_client)]) -> list[DeviceView]:
        """Registered hardware. *Devices* are the tank's; clients are people's."""
        return [DeviceView.model_validate(row) for row in await store.list_devices()]

    @app.get(
        "/api/v1/sensors",
        response_model=list[DeviceView],
        tags=["hardware"],
        operation_id="listSensors",
        responses={401: AUTH_401},
    )
    async def sensors(_: Annotated[UUID, Depends(current_client)]) -> list[DeviceView]:
        return [DeviceView.model_validate(row) for row in await store.list_devices(kind="sensor")]

    @app.patch(
        "/api/v1/devices/{device_id}",
        response_model=DeviceNameView,
        tags=["hardware"],
        operation_id="renameDevice",
        responses={401: AUTH_401, 404: {"description": "No such device."}},
    )
    async def rename_device(
        device_id: str,
        body: DeviceName,
        _: Annotated[UUID, Depends(current_client)],
    ) -> DeviceNameView:
        """Name a device, or clear the name.

        PATCH rather than PUT: this changes one operator-owned field on a row
        the hub otherwise maintains from hardware announcements, and a PUT would
        imply the client is supplying the whole device.
        """
        row = await store.set_display_name(device_id, body.display_name)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device")
        await sink("device.renamed", {"device_id": device_id, "display_name": body.display_name})
        return DeviceNameView(device_id=row["device_id"], display_name=row["display_name"])

    @app.get(
        "/api/v1/audit",
        response_model=list[AuditEvent],
        tags=["audit"],
        operation_id="listAudit",
        responses={401: AUTH_401},
    )
    async def list_audit(
        _: Annotated[UUID, Depends(current_client)],
        limit: int = 50,
        category: str | None = None,
    ) -> list[AuditEvent]:
        """The audit trail, as persisted.

        Reads `audit_log`, not the stream. `BR_AUDIT` is a delivery buffer that
        expires; Postgres is the system of record, and this endpoint is what
        makes "the event was recorded" checkable from outside the hub — which is
        how the writer's absence stayed invisible for as long as it did.
        """
        return [
            AuditEvent(
                message_id=str(row["message_id"]),
                occurred_at=row["occurred_at"],
                category=row["category"],
                actor=row["actor"],
                subject=row["subject"],
                device_id=row["device_id"],
                event=row["event"] if isinstance(row["event"], dict) else json.loads(row["event"]),
            )
            for row in await store.recent_audit(limit=limit, category=category)
        ]

    @app.get(
        "/api/v1/history",
        response_model=HistoryView,
        tags=["history"],
        operation_id="history",
        responses={
            401: AUTH_401,
            422: {"description": "Unusable window."},
            503: {"description": "No telemetry store configured."},
        },
    )
    async def history(
        _: Annotated[UUID, Depends(current_client)],
        start: datetime,
        end: datetime,
        buckets: int = DEFAULT_BUCKETS,
    ) -> HistoryView:
        """Downsampled history for every registered device, plus alert episodes.

        Downsampled *server-side* on purpose: a day of 5-second samples is 17k
        points per probe, and a phone drawing a 44pt chart cannot use them. What
        it can lose by downsampling is the spike that caused an alert, so every
        bucket carries its min/avg/max envelope rather than a single average.
        """
        if reader is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "no telemetry store configured"
            )
        if end <= start:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end must be after start")

        wanted = max(1, min(buckets, MAX_BUCKETS))
        bucket_s = HistoryReader.bucket_seconds(start, end, wanted)

        series: list[HistorySeries] = []
        for device in await store.list_devices():
            if device["kind"] == "sensor":
                metric, unit = "bellasreef_sensor_reading", "degC"
            else:
                # Duty is dimensionless 0–1; the client renders it as a
                # percentage. Saying "ratio" beats an empty string, which reads
                # as a missing field rather than a deliberate one.
                metric, unit = "bellasreef_actuator_level", "ratio"
            found = await reader.series(
                metric=metric,
                device_id=device["device_id"],
                unit=unit,
                start=start,
                end=end,
                buckets=wanted,
            )
            if found.buckets:
                series.append(
                    HistorySeries(
                        device_id=found.device_id,
                        metric=found.metric,
                        unit=found.unit,
                        buckets=[
                            HistoryBucket(
                                at=b.at,
                                minimum=b.minimum,
                                average=b.average,
                                maximum=b.maximum,
                            )
                            for b in found.buckets
                        ],
                    )
                )

        episodes = [
            HistoryEpisode(
                device_id=row["device_id"],
                sensor_type=row["sensor_type"],
                alert_class="silence" if row["alert_class"] == "silence" else "threshold",
                bound=None if row["bound"] is None else ("min" if row["bound"] == "min" else "max"),
                threshold=row["threshold"],
                unit=row["unit"],
                raised_at=row["raised_at"],
                raised_value=row["raised_value"],
                last_reading_at=row["last_reading_at"],
                cleared_at=row["cleared_at"],
                cleared_value=row["cleared_value"],
            )
            for row in await store.alert_episodes_between(start, end)
        ]

        return HistoryView(
            start=start, end=end, bucket_s=bucket_s, series=series, episodes=episodes
        )

    # ------------------------------------------------------------- alerts

    @app.get(
        "/api/v1/devices/{device_id}/thresholds",
        response_model=AlertThresholdsView,
        tags=["alerts"],
        operation_id="getThresholds",
        responses={401: AUTH_401, 404: {"description": "No such device."}},
    )
    async def get_thresholds(
        device_id: str, _: Annotated[UUID, Depends(current_client)]
    ) -> AlertThresholdsView:
        row = await store.thresholds_for(device_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device")
        return AlertThresholdsView(
            device_id=row["device_id"],
            minimum=row["alert_min"],
            maximum=row["alert_max"],
            clear_margin=row["alert_clear_margin"],
        )

    @app.put(
        "/api/v1/devices/{device_id}/thresholds",
        response_model=AlertThresholdsView,
        tags=["alerts"],
        operation_id="setThresholds",
        responses={
            401: AUTH_401,
            404: {"description": "No such device."},
            409: {"description": "Thresholds are a sensor concept."},
        },
    )
    async def put_thresholds(
        device_id: str,
        body: AlertThresholds,
        _: Annotated[UUID, Depends(current_client)],
    ) -> AlertThresholdsView:
        """Set or clear the band. The engine picks up the change within seconds.

        A PUT rather than a PATCH: the band is one setting with interdependent
        parts, and a partial update would let a client raise the minimum above
        the maximum in two legal-looking requests.
        """
        existing = await store.thresholds_for(device_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device")
        if existing["kind"] != "sensor" and (body.minimum is not None or body.maximum is not None):
            raise HTTPException(status.HTTP_409_CONFLICT, "thresholds can only be set on a sensor")

        row = await store.set_thresholds(
            device_id,
            minimum=body.minimum,
            maximum=body.maximum,
            clear_margin=body.clear_margin,
        )
        if row is None:  # pragma: no cover - the row existed a statement ago
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device")

        await sink(
            "thresholds.set",
            {
                "device_id": device_id,
                "minimum": body.minimum,
                "maximum": body.maximum,
                "clear_margin": body.clear_margin,
            },
        )
        return AlertThresholdsView(
            device_id=row["device_id"],
            minimum=row["alert_min"],
            maximum=row["alert_max"],
            clear_margin=row["alert_clear_margin"],
        )

    @app.get(
        "/api/v1/alerts",
        response_model=AlertsView,
        tags=["alerts"],
        operation_id="listAlerts",
        responses={401: AUTH_401},
    )
    async def list_alerts(
        _: Annotated[UUID, Depends(current_client)],
        limit: int = 50,
    ) -> AlertsView:
        """What is wrong now, and what has been wrong lately.

        This is also the reconnect path. Alerts are published on core pub/sub
        with no replay, so a client that was asleep during a breach learns about
        it here rather than from the stream.
        """
        return AlertsView(
            active=[_alert_view(row) for row in await alerts.active()],
            recent=[_alert_view(row) for row in await alerts.recent(limit=limit)],
        )

    # ---------------------------------------------------------- overrides

    @app.get(
        "/api/v1/overrides",
        response_model=list[OverrideView],
        tags=["overrides"],
        operation_id="listOverrides",
        responses={401: AUTH_401},
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
        responses={
            401: AUTH_401,
            409: {"description": "The target does not accept commands."},
            503: {"description": "Clock not synchronised; deadline would be wrong."},
        },
    )
    async def create_override(
        body: OverrideRequest, actor: Annotated[UUID, Depends(current_client)]
    ) -> OverrideView:
        """Hold a target at a level for a duration.

        Clock-gated: an override IS a deadline, and one computed from a clock
        chrony is about to step is not the duration the operator asked for.

        Authority-gated: an ``observe_only`` device is refused here, at the
        boundary where a command enters the system (device-classes.md §2.3).
        Filtering it further down would mean the command existed, was journaled,
        and was dropped by a component that happened to know better — and the
        operator would be told it had been placed.
        """
        _, authority = await store.control_authority_of(body.target)
        if authority == "observe_only":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{body.target!r} is registered observe_only and accepts no commands",
            )

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
        responses={
            401: AUTH_401,
            404: {"description": "Unknown or already released."},
        },
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

    vm_url = os.environ.get("BELLASREEF_VM_URL")
    if not vm_url:
        # Loud, for the same reason as the audit sink above: telemetry that is
        # silently not written looks exactly like a tank with nothing to report.
        log.critical(
            "BELLASREEF_VM_URL is unset: telemetry will NOT reach VictoriaMetrics "
            "and no history will be recorded"
        )

    return build_app(
        create_async_engine(dsn, future=True),
        audit=sink,
        nats_url=nats_url,
        vm_url=vm_url,
    )

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""API service — auth and pairing surface (auth.md).

Stateless front door. This pass implements discovery, pairing, tokens and
client management only; the WebSocket stream and the sensor/override surface
land next.

The unauthenticated set is exactly `/healthz`, `/api/v1/info`, `POST
/api/v1/pair`, `GET /api/v1/pair/{id}` and `POST /api/v1/token`, per auth.md §2.
Note that `POST /api/v1/pair/claim` is NOT in it: approving is what a bearer
token buys, and it is the whole reason the pairing code needs no rate limiter.
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
import secrets
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from time import monotonic
from typing import Annotated, Any, Final, Literal, Protocol
from uuid import UUID, uuid4

from bellasreef_contracts import DeviceAssignment
from bellasreef_db import (
    AlertRecord,
    ClockUntrustedError,
    OverrideStore,
    PostgresAlertStore,
    Transition,
)
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
from bellasreef_api.registry import AssignmentPublisher, CapabilityConsumer, RegistryConsumer
from bellasreef_api.security import (
    ACCESS_TOKEN_TTL_S,
    TokenError,
    hash_setup_code,
    issue_access_token,
    verify_access_token,
)
from bellasreef_api.store import PAIRING_TTL_S, ChannelHeldError, Store
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

#: How stale an open stream's authorization may get. Checked in the send
#: loop, so a revoked device stops receiving within one frame or this many
#: seconds, whichever is later. A recheck is one indexed SELECT; at ~1 Hz
#: telemetry this is one extra query per client per ten seconds.
STREAM_REVOKE_RECHECK_S: Final = 10.0


class AuditSink(Protocol):
    """Every audit event goes through one of these, per auth.md §3.

    ``category`` defaults to ``"auth"`` — a plain ``Callable`` type alias
    cannot express an optional trailing parameter, and most call sites
    (pairing, tokens, revocation) are content with the default. Device
    lifecycle and override sites pass ``category`` explicitly; see the
    call-site mapping fixed in :mod:`bellasreef_api.audit`.
    """

    async def __call__(self, event: str, detail: dict[str, Any], category: str = ...) -> None: ...


async def _noop_audit(event: str, detail: dict[str, Any], category: str = "auth") -> None:
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
    #: True until the first client has ever paired, by any method (spec
    #: 2026-08-15, Feature 1). Drives the app's connect screen: while this is
    #: true the client offers a setup-code entry field instead of the
    #: request-and-wait flow. Derived from ``Store.setup_state()`` rather than
    #: from ``pairing_open`` above — the two answer different questions and a
    #: hub can (briefly) have one true and not the other.
    setup_mode: bool


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
    #: The event name from the payload ("device.unbound", "pair.window_used"),
    #: promoted to a typed field so clients render verbs, not subjects.
    action: str | None


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

    #: The binding's capability channel — a PWM channel number or a 1-Wire
    #: ROM. ``None`` for a device whose binding is released: two adopted
    #: lights are otherwise indistinguishable except by name (David's ruling
    #: 2026-08-13). Additive and optional — no existing client breaks.
    channel: str | None = None

    #: False for a detached row: unbound, channel released, history kept.
    #: Clients section on this, not on channel being null.
    adopted: bool

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
    #: Setup-mode bootstrap (spec 2026-08-15). Non-null outside setup mode is
    #: an error, never ignored.
    setup_code: str | None = Field(default=None, max_length=16)


class PairGranted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str
    client_id: UUID


class PairPending(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    #: Six digits, zero-padded, for the operator to read off this device's
    #: screen and type into an already-paired one. A string, not an int: the
    #: leading zero in "042913" is part of what they type.
    #:
    #: The id above and this code answer different questions. The id is how this
    #: device follows its own request; the code is how a human points at it. Only
    #: the second can travel by eye, which is why v1 — approve by id — was
    #: uncompletable rather than merely unimplemented.
    pairing_code: str
    poll_after_s: int
    expires_in_s: int


class PairClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Validated here so a malformed entry is a 422 naming the field rather than
    #: a 404 that reads as "wrong code" — different advice to the operator.
    code: str = Field(pattern=r"^[0-9]{6}$")


class PairClaimed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


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
    #: How the light moves to this level and back: "snap" (one step) or
    #: "ramp" (the engine's global slew). Governs both ends of the hold —
    #: arrival and release/expiry (spec 2026-08-17). Defaults to "ramp",
    #: which is what every client before 3.8.0 got.
    transition: Transition = "ramp"


class OverrideView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    target: str
    duty: float
    expires_at: datetime
    expires_in_s: float
    transition: Transition


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


class _SetupThrottle:
    """10 failed setup-code attempts per rolling minute, globally.

    Module-level and in-process by ruling (spec 2026-08-15, Feature 1,
    "Throttling"): "a restart resetting the throttle is acceptable at this
    threat model." No database table, no counters that outlive the process —
    this is a hobbyist reef controller on a home LAN, not an enterprise
    product guarding against a patient adversary. A restart or a redeploy
    simply reopens the ten-per-minute budget, which is fine here.

    Deliberately module-level rather than per-app-instance: the whole point
    is one shared budget across every ``POST /pair`` call this process
    serves, the same way there is one hub. A counter scoped to `build_app`
    would reset on nothing that actually threatens the intended limit.
    """

    def __init__(self, limit: int = 10, window_s: float = 60.0) -> None:
        self._failures: deque[float] = deque()
        self._limit = limit
        self._window_s = window_s

    def retry_after(self, now: float) -> int | None:
        """Seconds until another attempt is allowed, or ``None`` if one is."""
        while self._failures and now - self._failures[0] > self._window_s:
            self._failures.popleft()
        if len(self._failures) < self._limit:
            return None
        return int(self._window_s - (now - self._failures[0])) + 1

    def record_failure(self, now: float) -> None:
        self._failures.append(now)


#: One throttle for the process, per the docstring above.
_setup_throttle: Final = _SetupThrottle()


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
    assignments = AssignmentPublisher(nats_url) if nats_url else None
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

        # Republish every assignment from Postgres, which is the source. The
        # retained stream is a cache of it, and this is the line that makes
        # that true: without it a restored or purged stream leaves hardware-io
        # building nothing while the devices table looks perfect.
        if assignments is not None:
            try:
                for row in await store.adopted_assignments():
                    await assignments.publish(
                        DeviceAssignment(
                            message_id=uuid4(),
                            emitted_at=datetime.now(UTC),
                            source="api",
                            device_id=row["device_id"],
                            adopted=True,
                            role=row["role"],
                            driver_type=row["driver_type"],
                            binding=row["binding"],
                        )
                    )
            except Exception:
                # Best effort: a hub that cannot reconcile should still serve.
                # hardware-io keeps whatever the stream already held, which is
                # the status quo rather than a regression.
                log.exception("could not reconcile device assignments")

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
        _, completed_at = await store.setup_state()
        return Info(
            name="Bella's Reef",
            api_version=API_VERSION,
            contracts_version=CONTRACTS_VERSION,
            paired_client_count=total,
            pairing_open=total == 0,
            approvers_available=await store.active_client_count() > 0,
            setup_mode=completed_at is None,
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
            409: {"description": "The recovery window was spent by another client."},
            422: {
                "description": "A setup code was missing/wrong in setup mode, or supplied "
                "outside setup mode. Never silently ignored."
            },
            429: {"description": "Too many failed setup-code attempts. See Retry-After."},
        },
    )
    async def pair(body: PairRequest, response: Response) -> PairGranted | PairPending:
        """Pair a client.

        A setup code, if present, is resolved first (spec 2026-08-15,
        Feature 1): valid in setup mode grants immediately; anything else
        about it is a 422, never a pending request nor a silent ignore — see
        the checks at the top of the body.

        Absent a setup code, an open recovery window is consulted next —
        `bellasreef pair` is a fire escape and must keep working even while
        setup is incomplete and a code has been minted (review ruling,
        2026-08-15: the spec's "window flow... still works during setup
        mode" wins over blind TOFU yielding to a minted code). Only once no
        window is open does a minted-but-unsupplied code become a rejection.

        The remaining outcomes then follow, in this order:

        1. No client has ever paired -> TOFU grant, and the window shuts.
        2. A recovery window is open -> grant and spend it.
        3. Someone is alive to approve -> 202, poll for a decision.
        4. Clients exist but every one is revoked -> 403. There is nobody to
           approve, so the honest answer is to tell the operator to run
           `bellasreef pair` on the hub rather than leave them polling a
           request no one will ever see.

        Outcome 2 has a fifth ending: losing the race for the window is a 409.
        """
        code_hash, completed_at = await store.setup_state()
        in_setup = completed_at is None

        if body.setup_code is not None:
            if not in_setup:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "This hub is already set up. Pair from an already-paired device, "
                    "or run `bellasreef pair` on the hub.",
                )
            if (after := _setup_throttle.retry_after(monotonic())) is not None:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Too many attempts - wait a minute.",
                    headers={"Retry-After": str(after)},
                )
            # compare_digest, not `!=`: the comparison is over a hash rather
            # than the code itself, but a hub answers this endpoint
            # unauthenticated and there is no reason to leak the shape of the
            # miss in the response time.
            if code_hash is None or not secrets.compare_digest(
                hash_setup_code(body.setup_code), code_hash
            ):
                _setup_throttle.record_failure(monotonic())
                await sink("pair.code_rejected", {"client_name": body.client_name})
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "That setup code is not right. It is on the deploy output on the "
                    "hub; dashes and case do not matter.",
                )
            # Valid: grant exactly as the TOFU/window paths do below (same
            # store call, not a second way to mint a client+token). House
            # naming (parallel to pair.tofu_granted/pair.window_used/
            # pair.approved) rather than the spec's illustrative
            # `client.paired` — one audit event per grant, not a second event
            # name layered on top of it.
            client_id, token = await store.create_client(body.client_name)
            await store.complete_setup()
            await sink(
                "pair.code_granted",
                {
                    "client_id": str(client_id),
                    "client_name": body.client_name,
                    "method": "setup_code",
                },
            )
            return PairGranted(refresh_token=token, client_id=client_id)

        # Code-less path. A recovery window, if one is open, is consulted
        # before the setup-code gate below — see the docstring.
        window_id = await store.open_window()
        if window_id is not None:
            client_id, token = await store.create_client(body.client_name)
            # The credential is minted before the window is spent, so a lost
            # race leaves a client row whose token has not left this process.
            # `consume_window` is atomic and returns False to the loser; the
            # boolean used to be discarded, which meant two concurrent calls
            # during one five-minute recovery window both walked away with a
            # permanent refresh token. The window is one credential (auth.md §1)
            # — so the loser's row is rolled back and it gets a 409, not a
            # second key to the tank.
            if not await store.consume_window(window_id, client_id):
                await store.discard_client(client_id)
                log.warning(
                    "lost the race for a recovery pairing window",
                    extra={"window_id": str(window_id), "event": "pair_window_race"},
                )
                # No `pair.window_used` for the loser: exactly one client used
                # this window, and the trail must say so once.
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "the recovery pairing window was spent by another client. Run "
                    "`bellasreef pair` on the hub to open another.",
                )
            await store.complete_setup()
            await sink(
                "pair.window_used",
                {
                    "window_id": str(window_id),
                    "client_id": str(client_id),
                    "client_name": body.client_name,
                    "method": "window",
                },
            )
            log.warning(
                "recovery pairing window used and now spent",
                extra={"window_id": str(window_id), "client_id": str(client_id)},
            )
            return PairGranted(refresh_token=token, client_id=client_id)

        if in_setup and code_hash is not None:
            # A code has been minted and no window is open; blind TOFU
            # yields to the code. Spec: a missing code in setup mode is an
            # explicit rejection, not a pending request — there is nobody
            # yet to approve it.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "This hub is in setup. Enter the setup code from the deploy output.",
            )

        total = await store.total_clients_ever()

        if total == 0:
            # Blind TOFU: reachable only when no setup code was ever minted
            # (the check above), so a hub deployed without the new deploy
            # step still bootstraps exactly as before.
            client_id, token = await store.create_client(body.client_name)
            await store.complete_setup()
            await sink(
                "pair.tofu_granted",
                {
                    "client_id": str(client_id),
                    "client_name": body.client_name,
                    # Not one of the spec's three method values
                    # ("setup_code" | "window" | "approval") — a recorded
                    # amendment (review ruling, 2026-08-15) rather than a
                    # fourth duplicate audit row per pairing.
                    "method": "tofu",
                },
            )
            log.warning(
                "TOFU window used and now closed",
                extra={"client_id": str(client_id), "event": "pair_tofu"},
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

        request_id, code = await store.open_pairing_request(body.client_name)
        await sink(
            "pair.requested",
            {"request_id": str(request_id), "client_name": body.client_name},
        )
        response.status_code = status.HTTP_202_ACCEPTED
        # The code is deliberately absent from the audit detail. It is a
        # selector, not a credential, but the trail is readable through
        # /api/v1/audit and a code sitting in it outlives the five minutes it
        # means anything for.
        return PairPending(
            request_id=request_id,
            pairing_code=code,
            poll_after_s=5,
            expires_in_s=PAIRING_TTL_S,
        )

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

    async def _approve(request_id: UUID, approver: UUID) -> None:
        """Approve a pending request and hold its credential for the poller.

        One implementation, two doors: by id (`/approve`) and by code
        (`/claim`). Claim is a resolver in front of this, not a second approval
        path — two of them would be two places for the audit event, the
        one-credential rule and the 409 to drift apart.
        """
        result = await store.approve_pairing(request_id)
        if result is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "pairing request is not pending")
        client_id, token = result
        pending.put(request_id, client_id, token)
        await store.complete_setup()
        await sink(
            "pair.approved",
            {
                "request_id": str(request_id),
                "client_id": str(client_id),
                "approved_by": str(approver),
                "method": "approval",
            },
        )

    @app.post(
        "/api/v1/pair/claim",
        response_model=PairClaimed,
        tags=["pairing"],
        operation_id="claimPairing",
        responses={
            401: AUTH_401,
            404: {"description": "No pairing request carries that code."},
            409: {"description": "That pairing request is no longer pending."},
        },
    )
    async def claim(
        body: PairClaim, approver: Annotated[UUID, Depends(current_client)]
    ) -> PairClaimed:
        """Approve by typing the six digits the new device is showing.

        auth.md §2 step 3a. This is the whole second-device journey from the
        operator's side: they hold the new device, read its code, and type it
        into the one already paired. Nothing has to tell them a request is
        waiting, and nothing has to identify the asking device — they are
        looking at it.

        No rate limiter and no attempt counter, deliberately. The bearer token
        above is the gate: only an already-paired client can approve anything, so
        guessing a code gains an attacker nothing they do not already hold. The
        code is a selector, not a credential.
        """
        found = await store.pairing_request_for_code(body.code)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no pairing request carries that code")
        request_id, state = found
        if state != "pending":
            # Including expired. 409 rather than 404 on purpose: the code was
            # right, so "check what you typed" would be wrong advice — the thing
            # to do is start again on the new device.
            raise HTTPException(
                status.HTTP_409_CONFLICT, "that pairing request is no longer pending"
            )
        await _approve(request_id, approver)
        return PairClaimed(request_id=request_id)

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
        await _approve(request_id, approver)
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

        # Announced after the row is stored. A binding that is stored and not
        # announced is recoverable; announced and not stored is not.
        if assignments is not None:
            await assignments.publish(
                DeviceAssignment(
                    message_id=uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="api",
                    device_id=device_id,
                    adopted=True,
                    role=body.role,
                    driver_type=body.driver_type,
                    binding=binding,
                )
            )

        await sink(
            "device.bound",
            {
                "device_id": device_id,
                "driver_type": body.driver_type,
                "channel": body.channel,
                "created": created,
            },
            category="config",
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
        await sink(
            "device.renamed",
            {"device_id": device_id, "display_name": body.display_name},
            category="config",
        )
        return DeviceNameView(device_id=row["device_id"], display_name=row["display_name"])

    @app.delete(
        "/api/v1/devices/{device_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["hardware"],
        operation_id="unbindDevice",
        responses={
            204: {"description": "Unbound. The channel is free to bind again."},
            401: AUTH_401,
            404: {"description": "No such device, or it is already unbound."},
        },
    )
    async def unbind_device(
        device_id: str,
        _: Annotated[UUID, Depends(current_client)],
    ) -> Response:
        """Release a device's claim on its hardware channel.

        **Soft, and DELETE anyway.** From the operator's side this is the delete:
        the device stops existing as a claim on a channel, which is the only
        thing the verb has to mean here. Underneath, the row survives with its
        name, thresholds, alert history and every telemetry series keyed on its
        `device_id`. Dropping the row would sever history from the hardware that
        produced it, and re-binding the same probe tomorrow would then look like
        new hardware — which is exactly the identity fork, reached from the other
        end.

        Without this endpoint a PWM channel bound to the wrong device was taken
        for good: `bindDevice` returns 409 on an actuator channel someone else
        holds, and nothing anywhere could let go. SQL on the hub was the only
        way out, which is also the way an audit row went missing.
        """
        row = await store.unadopt_device(device_id)
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no adopted device {device_id!r}. It was never bound, or it is already unbound.",
            )

        # The tombstone the contract defines: adopted=False republished on the
        # device's own subject, rather than a deleted subject. A deletion simply
        # vanishes, and a hardware-io that was offline for it would come back
        # believing the device is still its to build.
        #
        # driver_type and binding ride along even though only `adopted` is read.
        # They cost nothing, the model permits them when adopted is False, and
        # the retained last value on this subject is then a record of which
        # channel was released rather than only that something was.
        if assignments is not None:
            await assignments.publish(
                DeviceAssignment(
                    message_id=uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="api",
                    device_id=row["device_id"],
                    adopted=False,
                    role=row["role"],
                    driver_type=row["driver_type"],
                    binding=row["binding"],
                )
            )

        await sink(
            "device.unbound",
            {
                "device_id": row["device_id"],
                "driver_type": row["driver_type"],
                "binding": row["binding"],
            },
            category="config",
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/v1/devices/{device_id}/readopt",
        response_model=DeviceView,
        tags=["hardware"],
        operation_id="readoptDevice",
        responses={
            200: {"description": "Re-adopted onto the channel its row remembers."},
            401: AUTH_401,
            404: {"description": "No such device, or it is not detached."},
            409: {"description": "Its channel is now held by another adopted device."},
        },
    )
    async def readopt_device(
        device_id: str,
        _: Annotated[UUID, Depends(current_client)],
    ) -> DeviceView:
        """Reattach a detached device to the channel its row still remembers.

        The Detached section's "re-add" (ruled 2026-08-15). `unbind_device`
        keeps the row and its binding on purpose, and this is what makes that
        worth doing rather than merely quiet: the operator gets the *same*
        device back — name, thresholds and history intact — instead of
        re-binding through `bindDevice` and hoping the proposed id is the one
        that lands (it is not always; see `bind_device`'s matching note).
        """
        try:
            row = await store.readopt_device(device_id)
        except ChannelHeldError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{device_id!r}'s channel is now held by {exc.holder!r}. Unbind it first.",
            ) from exc
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no detached device {device_id!r}. It is unknown, or already adopted.",
            )

        # Same tombstone shape as unbind_device, adopted=True this time: the
        # retained last value on this subject is what lets a hardware-io that
        # was offline for the readopt still build the driver on its next start.
        if assignments is not None:
            await assignments.publish(
                DeviceAssignment(
                    message_id=uuid4(),
                    emitted_at=datetime.now(UTC),
                    source="api",
                    device_id=row["device_id"],
                    adopted=True,
                    role=row["role"],
                    driver_type=row["driver_type"],
                    binding=row["binding"],
                )
            )

        await sink(
            "device.bound",
            {"device_id": device_id, "readopt": True},
            category="config",
        )
        full = next(d for d in await store.list_devices() if d["device_id"] == device_id)
        return DeviceView.model_validate(full)

    @app.post(
        "/api/v1/devices/{device_id}/forget",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["hardware"],
        operation_id="forgetDevice",
        responses={
            204: {"description": "Deleted. Identity and settings are gone."},
            401: AUTH_401,
            404: {"description": "No such device."},
            409: {"description": "Still adopted. Unbind it first."},
        },
    )
    async def forget_device(
        device_id: str,
        _: Annotated[UUID, Depends(current_client)],
    ) -> Response:
        """Delete a detached device row for good.

        The Detached section's "clear" (ruled 2026-08-15). `unbind_device`'s
        docstring explains why deletion is normally the wrong move: it severs
        history from hardware that might come back. This endpoint is the
        operator overruling that on purpose — the hardware is gone, and the
        name should stop appearing. Telemetry already written keeps its
        device_id; nothing here rewrites history.

        No assignment publish: a detached device holds no channel claim to
        retract, and `unbind_device`'s tombstone already recorded the release.
        """
        outcome = await store.forget_device(device_id)
        if outcome == "missing":
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no device {device_id!r}.")
        if outcome == "adopted":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{device_id!r} is adopted. Unbind it first.",
            )
        await sink("device.forgotten", {"device_id": device_id}, category="config")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        events = []
        for row in await store.recent_audit(limit=limit, category=category):
            payload = row["event"] if isinstance(row["event"], dict) else json.loads(row["event"])
            events.append(
                AuditEvent(
                    message_id=str(row["message_id"]),
                    occurred_at=row["occurred_at"],
                    category=row["category"],
                    actor=row["actor"],
                    subject=row["subject"],
                    device_id=row["device_id"],
                    event=payload,
                    action=(payload or {}).get("event"),
                )
            )
        return events

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
            category="config",
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
                transition=o.transition,
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
            placement = await overrides.create(
                body.target,
                body.duty,
                body.duration_s,
                reason=body.reason,
                transition=body.transition,
            )
        except ClockUntrustedError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        placed = placement.override

        # A supersede is an ending. Record it before the new beginning, in the
        # same event a manual release writes, so the log reads as one trail —
        # before this the store closed the old hold as 'superseded' and the
        # trail showed three "started" and no "ended" for one light re-held
        # twice (UX review 2026-08-17, E1). The store names what it displaced
        # atomically with the insert, so this can never blame the wrong hold.
        for displaced in placement.superseded:
            await sink(
                "override.released",
                {
                    "override_id": str(displaced.id),
                    "target": displaced.target,
                    "reason": "superseded",
                    "superseded_by": str(placed.id),
                    "actor": str(actor),
                },
                category="command",
            )
        await sink(
            "override.created",
            {
                "override_id": str(placed.id),
                "target": placed.target,
                "duty": placed.duty,
                "expires_at": placed.expires_at.isoformat(),
                "actor": str(actor),
                "transition": placed.transition,
            },
            category="command",
        )
        return OverrideView(
            id=placed.id,
            target=placed.target,
            duty=placed.duty,
            expires_at=placed.expires_at,
            expires_in_s=round(body.duration_s, 1),
            transition=placed.transition,
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
            {"override_id": str(override_id), "reason": "manual", "actor": str(actor)},
            category="command",
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

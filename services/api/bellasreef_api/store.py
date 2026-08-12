# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Database operations for pairing and tokens.

Raw SQL against schema 0003 rather than the ORM: these are a handful of
statements whose exact shape matters (the revocation update must clear the hash
and stamp the timestamp in one statement, or the CHECK constraint rejects it),
and the constraints are doing real work that an ORM layer would only obscure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bellasreef_api.security import hash_refresh_token, new_refresh_token, new_signing_secret

__all__ = ["ClientRow", "PairingOutcome", "Store"]

#: auth.md §2: a pairing request lives 5 minutes.
PAIRING_TTL_S = 300


@dataclass(frozen=True, slots=True)
class ClientRow:
    id: UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class PairingOutcome:
    """Either an immediate pairing, or a request to poll on."""

    status: str  # "paired" | "pending" | "closed"
    refresh_token: str | None = None
    client_id: UUID | None = None
    request_id: UUID | None = None


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------ signing key

    async def signing_secret(self) -> str:
        """The active signing key, generated on first call.

        Kept in Postgres rather than a file so restoring the database restores
        working sessions with it.
        """
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT secret FROM signing_keys WHERE retired_at IS NULL "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                )
            ).first()
            if row is not None:
                return str(row[0])

            secret = new_signing_secret()
            await conn.execute(
                text("INSERT INTO signing_keys (id, secret) VALUES (:id, :secret)"),
                {"id": uuid4(), "secret": secret},
            )
            return secret

    # ------------------------------------------------------------ identity

    async def hub_id(self) -> UUID:
        """This hub's identity, minted on first call and never again.

        Same lazy shape as :meth:`signing_secret`, and for the same reason: the
        migration deliberately does not seed a row. A migration runs on every
        hub *including one that is about to have a backup restored into it*, and
        stamping an id at migration time would give the restored database a new
        identity — destroying the very fact this table exists to carry.

        Written here, so a restore brings the original row back with the rest of
        the data and the archive still names the hub it came from.
        """
        async with self._engine.begin() as conn:
            row = (await conn.execute(text("SELECT id FROM hub_identity"))).first()
            if row is not None:
                return UUID(str(row[0]))

            hub = uuid4()
            # ON CONFLICT DO NOTHING, then re-read: two services starting
            # together would otherwise race, and the singleton unique
            # constraint would turn the loser's INSERT into a crash on boot.
            await conn.execute(
                text(
                    "INSERT INTO hub_identity (id, singleton) VALUES (:id, true) "
                    "ON CONFLICT (singleton) DO NOTHING"
                ),
                {"id": hub},
            )
            winner = (await conn.execute(text("SELECT id FROM hub_identity"))).first()
            return UUID(str(winner[0])) if winner is not None else hub

    # ------------------------------------------------------------ capabilities

    async def replace_capabilities(
        self, source: str, channels: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Store what one hardware source announced, replacing what it said before.

        Replace rather than merge. What the hardware reports is the truth, so a
        channel that has gone away must stop being offered — a merge would leave
        an operator able to bind a PCA9685 channel on a board that is no longer
        on the bus.

        Scoped to one source: a PWM announcement must not clear the 1-Wire bus.
        """
        async with self._engine.begin() as conn:
            if channels:
                await conn.execute(
                    text(
                        "DELETE FROM capabilities WHERE source = :source AND channel <> ALL(:keep)"
                    ),
                    {"source": source, "keep": [c for c, _ in channels]},
                )
            else:
                await conn.execute(
                    text("DELETE FROM capabilities WHERE source = :source"),
                    {"source": source},
                )

            for channel, detail in channels:
                await conn.execute(
                    text(
                        "INSERT INTO capabilities (id, source, channel, detail, announced_at) "
                        "VALUES (:id, :source, :channel, CAST(:detail AS jsonb), :now) "
                        "ON CONFLICT (source, channel) DO UPDATE "
                        "SET detail = EXCLUDED.detail, announced_at = EXCLUDED.announced_at"
                    ),
                    {
                        "id": uuid4(),
                        "source": source,
                        "channel": channel,
                        "detail": json.dumps(detail),
                        "now": datetime.now(UTC),
                    },
                )

    async def list_capabilities(self) -> list[dict[str, Any]]:
        """Every announced capability, with whether a device has claimed it.

        The bound flag is computed by joining rather than stored, because a
        binding lives on the device and storing a copy here would be two places
        that can disagree about whether a channel is free.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.source, c.channel, c.detail, c.announced_at, "
                        "       d.device_id AS bound_to "
                        "  FROM capabilities c "
                        "  LEFT JOIN devices d "
                        "         ON d.driver_type = c.source "
                        "        AND d.adopted "
                        "        AND COALESCE(d.binding ->> 'channel', d.binding ->> 'rom') "
                        "            = c.channel "
                        " ORDER BY c.source, c.channel"
                    )
                )
            ).mappings()
            return [dict(row) for row in rows]

    async def device_bound_to(self, driver_type: str, channel: str) -> str | None:
        """Which device has claimed this capability channel, if any."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT device_id FROM devices "
                        " WHERE adopted AND driver_type = :driver_type "
                        "   AND COALESCE(binding ->> 'channel', binding ->> 'rom') = :channel"
                    ),
                    {"driver_type": driver_type, "channel": channel},
                )
            ).first()
        return str(row[0]) if row else None

    async def capability_exists(self, source: str, channel: str) -> bool:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM capabilities WHERE source = :source AND channel = :channel"
                    ),
                    {"source": source, "channel": channel},
                )
            ).first()
        return row is not None

    async def bind_device(
        self,
        *,
        device_id: str,
        kind: str,
        driver_type: str,
        channel: str,
        binding: dict[str, str],
        role: str | None,
        display_name: str | None,
        location: str | None,
        sensor_type: str | None,
        poll_interval_s: float | None,
    ) -> tuple[str, bool]:
        """Bind a capability channel to a device. Returns ``(device_id, created)``.

        **Matches before it creates**, and that is the whole point of this
        method. If a device already carries this binding — the same ROM, the same
        PWM channel — that device *is* this hardware, whatever id the caller
        proposed, and it is adopted and updated in place.

        This exists because the alternative happened. A seed naming a probe
        ``display-tank`` created a second device row beside the one already
        holding that probe's name, thresholds, alert history and a day of
        telemetry, and the tank's history forked in two. The ROM is the
        hardware's identity; the device_id is the registry's; a caller
        proposing a new id for known hardware is renaming at most, never
        creating.
        """
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    text(
                        "SELECT device_id FROM devices "
                        " WHERE driver_type = :driver_type "
                        "   AND COALESCE(binding ->> 'channel', binding ->> 'rom') = :channel"
                    ),
                    {"driver_type": driver_type, "channel": channel},
                )
            ).first()

            # A 1-Wire probe already in the registry from an announcement, keyed
            # on its own id, is the same hardware even though it has no binding
            # yet — that is the adopt path.
            if existing is None and sensor_type is not None:
                existing = (
                    await conn.execute(
                        text(
                            "SELECT device_id FROM devices "
                            " WHERE binding IS NULL AND kind = 'sensor' AND device_id = :id"
                        ),
                        {"id": device_id},
                    )
                ).first()

            target = str(existing[0]) if existing else device_id
            created = existing is None

            if created:
                await conn.execute(
                    text(
                        "INSERT INTO devices (id, device_id, kind, driver_id, sensor_type, "
                        "                     poll_interval_s, role, display_name, location, "
                        "                     driver_type, binding, adopted) "
                        "VALUES (:id, :device_id, :kind, :driver_id, :sensor_type, "
                        "        :poll_interval_s, :role, :display_name, :location, "
                        "        :driver_type, CAST(:binding AS jsonb), true) "
                        "ON CONFLICT (device_id) DO NOTHING"
                    ),
                    {
                        "id": uuid4(),
                        "device_id": target,
                        "kind": kind,
                        "driver_id": driver_type,
                        "sensor_type": sensor_type,
                        "poll_interval_s": poll_interval_s,
                        "role": role,
                        "display_name": display_name,
                        "location": location,
                        "driver_type": driver_type,
                        "binding": json.dumps(binding),
                    },
                )
            else:
                # Names its columns, and COALESCE on the operator-owned ones: a
                # seed re-run must not blank a name somebody typed. Same rule as
                # the sensor upsert — a re-announce cannot reset the operator's
                # choices.
                await conn.execute(
                    text(
                        "UPDATE devices "
                        "   SET driver_type = :driver_type, "
                        "       binding = CAST(:binding AS jsonb), "
                        "       adopted = true, "
                        "       role = COALESCE(:role, role), "
                        "       display_name = COALESCE(:display_name, display_name), "
                        "       location = COALESCE(:location, location) "
                        " WHERE device_id = :device_id"
                    ),
                    {
                        "device_id": target,
                        "driver_type": driver_type,
                        "binding": json.dumps(binding),
                        "role": role,
                        "display_name": display_name,
                        "location": location,
                    },
                )
        return target, created

    # ------------------------------------------------------------ clients

    async def total_clients_ever(self) -> int:
        """Every client row, revoked included.

        The TOFU window keys on this rather than on *active* clients, and the
        difference is a security hole: if it counted only live clients, an
        attacker who revoked everything would reopen the open-pairing window.
        Rows persist through revocation precisely so the window stays shut.
        """
        async with self._engine.connect() as conn:
            return int(
                (await conn.execute(text("SELECT count(*) FROM paired_clients"))).scalar_one()
            )

    async def list_clients(self) -> list[ClientRow]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT id, name, created_at, last_seen_at, revoked_at "
                    "FROM paired_clients ORDER BY created_at"
                )
            )
            return [ClientRow(*r) for r in rows.all()]

    async def create_client(self, name: str) -> tuple[UUID, str]:
        """Create a client and return ``(id, refresh_token)``.

        The token is returned once and never stored in the clear.
        """
        token = new_refresh_token()
        client_id = uuid4()
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO paired_clients (id, name, refresh_token_hash) "
                    "VALUES (:id, :name, :hash)"
                ),
                {"id": client_id, "name": name, "hash": hash_refresh_token(token)},
            )
        return client_id, token

    async def client_for_refresh_token(self, token: str) -> UUID | None:
        """Resolve a refresh token, or None if unknown or revoked.

        Revoked rows have a NULL hash, so they cannot match — the same query
        covers both cases without a special check to forget.
        """
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM paired_clients WHERE refresh_token_hash = :hash"),
                    {"hash": hash_refresh_token(token)},
                )
            ).first()
            if row is None:
                return None
            client_id = UUID(str(row[0]))
            await conn.execute(
                text("UPDATE paired_clients SET last_seen_at = :now WHERE id = :id"),
                {"now": datetime.now(UTC), "id": client_id},
            )
            return client_id

    async def is_active(self, client_id: UUID) -> bool:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT revoked_at FROM paired_clients WHERE id = :id"),
                    {"id": client_id},
                )
            ).first()
            return row is not None and row[0] is None

    async def revoke(self, client_id: UUID) -> bool:
        """Revoke a client. Returns False if it was unknown or already revoked.

        Clearing the hash and stamping revoked_at happen in one statement
        because the CHECK constraint requires exactly one of them to be NULL —
        doing it in two steps would transiently violate it.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE paired_clients SET refresh_token_hash = NULL, revoked_at = :now "
                    "WHERE id = :id AND revoked_at IS NULL"
                ),
                {"now": datetime.now(UTC), "id": client_id},
            )
            return bool(result.rowcount)

    async def active_client_count(self) -> int:
        """Clients that can still approve a pairing request.

        Distinct from :meth:`total_clients_ever`, and the distinction is the
        whole recovery problem: if every client is revoked, the TOFU-ever window
        is shut and there is nobody to approve a 202 — which is the deadlock the
        pairing window exists to break.
        """
        async with self._engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM paired_clients WHERE revoked_at IS NULL")
                    )
                ).scalar_one()
            )

    # ------------------------------------------------------------- hardware

    async def list_devices(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Registered hardware — sensors and actuators.

        Note the vocabulary: *devices* are the tank's, *clients* are people's.
        The split is why the client endpoints moved to /api/v1/clients.
        """
        sql = (
            "SELECT device_id, display_name, kind, driver_id, sensor_type, "
            "poll_interval_s, actuator_class, role, control_authority, "
            "failsafe_capable, transport, safe_state, max_runtime_s, "
            "heartbeat_timeout_s, enabled, alert_min, alert_max, alert_clear_margin "
            "FROM devices"
        )
        params: dict[str, object] = {}
        if kind is not None:
            sql += " WHERE kind = :kind"
            params["kind"] = kind
        sql += " ORDER BY device_id"

        async with self._engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def alert_episodes_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Episodes overlapping a window, for the chart's alert bands.

        Overlap, not containment: an episode that started before the window and
        is still open is exactly the one an operator most needs to see banded.
        An open episode (``cleared_at IS NULL``) is included and left open —
        the client clamps the band to the window edge rather than the API
        inventing a clear time.
        """
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT device_id, sensor_type, alert_class, bound, threshold, unit, "
                            "raised_at, raised_value, last_reading_at, "
                            "cleared_at, cleared_value "
                            "FROM sensor_alerts "
                            "WHERE raised_at <= :end "
                            "AND (cleared_at IS NULL OR cleared_at >= :start) "
                            "ORDER BY raised_at"
                        ),
                        {"start": start, "end": end},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def recent_audit(
        self, limit: int = 50, category: str | None = None
    ) -> list[dict[str, Any]]:
        """Most recent audit events, newest first."""
        sql = (
            "SELECT message_id, occurred_at, category, actor, subject, device_id, event "
            "FROM audit_log"
        )
        params: dict[str, object] = {"limit": limit}
        if category is not None:
            sql += " WHERE category = :category"
            params["category"] = category
        sql += " ORDER BY occurred_at DESC, id DESC LIMIT :limit"
        async with self._engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def device_labels(self, device_id: str) -> tuple[bool, str | None, str | None]:
        """``(known, control_authority, transport)`` for telemetry labelling.

        One round trip, because both labels are needed on the same series and a
        second query could observe a different row.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT control_authority, transport FROM devices "
                        "WHERE device_id = :device_id"
                    ),
                    {"device_id": device_id},
                )
            ).first()
        return (False, None, None) if row is None else (True, row[0], row[1])

    async def control_authority_of(self, device_id: str) -> tuple[bool, str | None]:
        """``(the hub knows this device, its declared authority)``.

        Two different absences, kept apart deliberately. A missing row means the
        hub has never heard of the device; a present row with a NULL authority
        means the device is a *sensor*, which carries none by construction
        (docs/device-classes.md §2 confines the axis to actuators). Collapsing
        them into one ``None`` made every alert on a probe read as
        ``control_authority="unknown"`` — which says the lookup failed, when in
        fact the answer is that the question does not apply.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT control_authority FROM devices WHERE device_id = :device_id"),
                    {"device_id": device_id},
                )
            ).first()
        return (False, None) if row is None else (True, row[0])

    async def upsert_sensor(
        self,
        *,
        device_id: str,
        driver_id: str,
        sensor_type: str,
        poll_interval_s: float,
        transport: str,
    ) -> bool:
        """Record a registered sensor. Returns True when the row was new.

        ON CONFLICT updates only what the hardware announces. It deliberately
        does not touch ``display_name`` or the alert thresholds: those belong to
        the operator, and a probe re-announcing itself after a hardware-io
        restart must not silently reset the name and the band somebody chose.
        """
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "INSERT INTO devices (id, device_id, kind, driver_id, sensor_type, "
                            "poll_interval_s, transport) VALUES (gen_random_uuid(), "
                            ":device_id, 'sensor', :driver_id, :sensor_type, "
                            ":poll_interval_s, :transport) "
                            "ON CONFLICT (device_id) DO UPDATE SET "
                            "driver_id = EXCLUDED.driver_id, "
                            "sensor_type = EXCLUDED.sensor_type, "
                            "poll_interval_s = EXCLUDED.poll_interval_s, "
                            "transport = EXCLUDED.transport, "
                            "updated_at = now() "
                            "RETURNING (xmax = 0) AS inserted"
                        ),
                        {
                            "device_id": device_id,
                            "driver_id": driver_id,
                            "sensor_type": sensor_type,
                            "poll_interval_s": poll_interval_s,
                            "transport": transport,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return bool(row["inserted"]) if row is not None else False

    async def set_display_name(self, device_id: str, name: str | None) -> dict[str, Any] | None:
        """Name a device, or clear the name back to NULL."""
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "UPDATE devices SET display_name = :name, updated_at = now() "
                            "WHERE device_id = :device_id "
                            "RETURNING device_id, kind, display_name, sensor_type"
                        ),
                        {"device_id": device_id, "name": name},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    async def thresholds_for(self, device_id: str) -> dict[str, Any] | None:
        """Alert configuration for one sensor, or ``None`` if no such device."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT device_id, kind, alert_min, alert_max, alert_clear_margin "
                            "FROM devices WHERE device_id = :device_id"
                        ),
                        {"device_id": device_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    async def set_thresholds(
        self,
        device_id: str,
        *,
        minimum: float | None,
        maximum: float | None,
        clear_margin: float | None,
    ) -> dict[str, Any] | None:
        """Write the band. Returns the stored row, or ``None`` for an unknown device.

        The CHECK constraints are the real validator. The API mirrors them so a
        bad request gets a 422 with a field name rather than a 500 with a
        constraint name, but the database is what makes them true — a future
        writer that is not this endpoint cannot bypass them.
        """
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "UPDATE devices SET alert_min = :minimum, alert_max = :maximum, "
                            "alert_clear_margin = :margin WHERE device_id = :device_id "
                            "RETURNING device_id, kind, alert_min, alert_max, alert_clear_margin"
                        ),
                        {
                            "device_id": device_id,
                            "minimum": minimum,
                            "maximum": maximum,
                            "margin": clear_margin,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    # ------------------------------------------------------- recovery window

    async def open_pairing_window(
        self, opened_by: str, ttl_s: float, *, now: datetime | None = None
    ) -> tuple[UUID, datetime]:
        """Reopen pairing for a bounded time. Used only by the recovery CLI."""
        opened = now or datetime.now(UTC)
        expires = opened + timedelta(seconds=ttl_s)
        window_id = uuid4()
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO pairing_windows (id, opened_at, expires_at, opened_by) "
                    "VALUES (:id, :opened, :expires, :by)"
                ),
                {"id": window_id, "opened": opened, "expires": expires, "by": opened_by},
            )
        return window_id, expires

    async def open_window(self, *, now: datetime | None = None) -> UUID | None:
        """An unused, unexpired window, if one exists."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id FROM pairing_windows "
                        "WHERE used_at IS NULL AND expires_at > :now "
                        "ORDER BY opened_at DESC LIMIT 1"
                    ),
                    {"now": now or datetime.now(UTC)},
                )
            ).first()
        return UUID(str(row[0])) if row else None

    async def consume_window(
        self, window_id: UUID, client_id: UUID, *, now: datetime | None = None
    ) -> bool:
        """Spend a window. One window is one credential, not a standing invite."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pairing_windows SET used_at = :now, used_by = :client "
                    "WHERE id = :id AND used_at IS NULL AND expires_at > :now"
                ),
                {"now": now or datetime.now(UTC), "client": client_id, "id": window_id},
            )
            return bool(result.rowcount)

    # ------------------------------------------------------------ pairing

    async def open_pairing_request(self, client_name: str) -> UUID:
        request_id = uuid4()
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO pairing_requests (id, client_name, state, expires_at) "
                    "VALUES (:id, :name, 'pending', :exp)"
                ),
                {
                    "id": request_id,
                    "name": client_name,
                    "exp": datetime.now(UTC) + timedelta(seconds=PAIRING_TTL_S),
                },
            )
        return request_id

    async def pairing_state(self, request_id: UUID) -> tuple[str, UUID | None, str | None]:
        """``(state, client_pk, client_name)``; state ``missing`` if unknown.

        Expiry is evaluated against the stored timestamp, so a pending request
        that has aged out reads as expired without a sweeper having run.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT state, client_pk, client_name, expires_at "
                        "FROM pairing_requests WHERE id = :id"
                    ),
                    {"id": request_id},
                )
            ).first()
        if row is None:
            return "missing", None, None
        state, client_pk, name, expires_at = row
        if state == "pending" and expires_at <= datetime.now(UTC):
            return "expired", None, name
        return str(state), (UUID(str(client_pk)) if client_pk else None), str(name)

    async def approve_pairing(self, request_id: UUID) -> tuple[UUID, str] | None:
        """Approve a pending request, creating its client.

        Returns ``(client_id, refresh_token)``, or None if the request was not
        pending — already decided, expired, or unknown.
        """
        state, _, name = await self.pairing_state(request_id)
        if state != "pending" or name is None:
            return None

        client_id, token = await self.create_client(name)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pairing_requests SET state = 'approved', client_pk = :pk, "
                    "decided_at = :now WHERE id = :id AND state = 'pending'"
                ),
                {"pk": client_id, "now": datetime.now(UTC), "id": request_id},
            )
            if not result.rowcount:
                # Lost a race with another approver. Roll the client back rather
                # than leave an orphan that can never be reached.
                await conn.execute(
                    text("DELETE FROM paired_clients WHERE id = :id"), {"id": client_id}
                )
                return None
        return client_id, token

    async def deny_pairing(self, request_id: UUID) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pairing_requests SET state = 'denied', decided_at = :now "
                    "WHERE id = :id AND state = 'pending'"
                ),
                {"now": datetime.now(UTC), "id": request_id},
            )
            return bool(result.rowcount)

    async def take_pairing_token(self, request_id: UUID) -> str | None:
        """Not stored: the token is handed to the poller at approval time.

        Kept as an explicit method so the one-shot nature is visible rather
        than implied — see the note in routes.py about why approval returns the
        token to the *approver* and the poller re-derives nothing.
        """
        return None

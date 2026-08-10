# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Database operations for pairing and tokens.

Raw SQL against schema 0003 rather than the ORM: these are a handful of
statements whose exact shape matters (the revocation update must clear the hash
and stamp the timestamp in one statement, or the CHECK constraint rejects it),
and the constraints are doing real work that an ORM layer would only obscure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Token minting and verification (auth.md §3).

Two token types, deliberately asymmetric:

**Refresh token** — opaque, 256-bit, stored only as a SHA-256 hash. It is the
long-lived credential and the only thing that can mint JWTs, so the database
never holds a usable copy. A dump of `paired_clients` gets an attacker nothing.

**Access JWT** — short-lived, signed with a key generated at first boot. Claims
are `client_id`, `iat`, `exp` and nothing else, because there are no roles to
claim.

The asymmetry is the revocation story: revoking deletes the hash, so no new
JWTs can be minted, while outstanding JWTs simply expire. There is no denylist
and no introspection endpoint — that machinery buys nothing at a ceiling of one
operator, and it would be one more thing to be wrong.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

__all__ = [
    "ACCESS_TOKEN_TTL_S",
    "REFRESH_TOKEN_BYTES",
    "SETUP_ALPHABET",
    "TokenError",
    "format_setup_code",
    "hash_refresh_token",
    "hash_setup_code",
    "issue_access_token",
    "new_refresh_token",
    "new_setup_code",
    "new_signing_secret",
    "normalize_setup_code",
    "verify_access_token",
]

#: 256 bits, per auth.md §3.
REFRESH_TOKEN_BYTES = 32

#: ~15 minutes. This is also the maximum exposure window after revocation:
#: outstanding JWTs die at exp, and nothing revokes them early.
ACCESS_TOKEN_TTL_S = 900

_ALGORITHM = "HS256"


class TokenError(Exception):
    """A token was absent, malformed, expired, or not ours."""


def new_refresh_token() -> str:
    """A fresh opaque refresh token. Returned once, never stored in the clear."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex of a refresh token.

    Plain SHA-256 rather than a password KDF on purpose: this is a 256-bit
    random value, not a human-chosen secret. There is no dictionary to attack,
    so the work factor a KDF buys would only slow down every legitimate token
    refresh.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def new_signing_secret() -> str:
    return secrets.token_urlsafe(48)


def issue_access_token(
    client_id: UUID,
    secret: str,
    *,
    ttl_s: int = ACCESS_TOKEN_TTL_S,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Mint a JWT. Returns ``(token, expires_in_seconds)``."""
    issued = now or datetime.now(UTC)
    expires = issued + timedelta(seconds=ttl_s)
    payload = {
        "client_id": str(client_id),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM), ttl_s


#: Spec 2026-08-15 (Feature 1, "Semantics"): "8 characters from a
#: confusable-free alphabet (Crockford base32 minus 0/O/1/I)". Crockford's
#: own 32-symbol alphabet already excludes I, L, O, U; "minus 0/O/1/I" on
#: top of that removes 0 and 1 (O and I are already gone), leaving 30
#: characters, not 32 minus 4. Implemented per the spec's literal words
#: rather than reasoning about what a "clean" confusable-free set would be.
SETUP_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ".translate(str.maketrans("", "", "0OI1"))


def new_setup_code() -> str:
    """8 chars, ~40 bits (log2(30**8) ~= 39.5). Returned once; only the hash
    is stored, so a lost code is answered by rotating (:func:`hash_setup_code`
    + :meth:`Store.set_setup_code_hash`), not by reprinting."""
    return "".join(secrets.choice(SETUP_ALPHABET) for _ in range(8))


def format_setup_code(code: str) -> str:
    """Grouped for display: ``7KF2-9QMD``. The dash is cosmetic — see
    :func:`normalize_setup_code`, which is what entry actually validates
    against."""
    return f"{code[:4]}-{code[4:]}"


def normalize_setup_code(entry: str) -> str:
    """Case-insensitive; the grouping dash is cosmetic and ignored (spec)."""
    return entry.replace("-", "").strip().upper()


def hash_setup_code(code: str) -> str:
    """Same construction as :func:`hash_refresh_token`: plain SHA-256 hex,
    no salt, no KDF — over the *normalized* code so ``7kf2-9qmd`` and
    ``7KF29QMD`` hash identically. Stored in ``hub_identity.setup_code_hash``;
    "I forgot" is answered by rotating (mint a new code, overwrite the hash),
    never by reprinting, so there is no plaintext anywhere to reprint from.
    """
    return hashlib.sha256(normalize_setup_code(code).encode()).hexdigest()


def verify_access_token(token: str, secret: str) -> UUID:
    """Return the client id, or raise :class:`TokenError`.

    ``algorithms`` is pinned to a single value. Accepting whatever the token's
    own header claims is the classic JWT hole — a token asking to be verified
    with ``none`` should not get a hearing.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid access token") from exc

    raw = payload.get("client_id")
    if not isinstance(raw, str):
        raise TokenError("token carries no client_id")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise TokenError("token client_id is not a uuid") from exc

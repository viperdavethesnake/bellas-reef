# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The guard that keeps the test suite off the hub.

A rule in a document is a rule people follow until the night they are tired.
This is the same rule expressed as an exit code.

The failure it prevents is not theoretical and not rare. Durables are shared
broker state, and BR_CMD is a workqueue that permits exactly one consumer per
filter subject — so a test binding a durable on the hub's NATS is contending
for the hub's own slot by construction. On 2026-08-10 a leaked test durable
held that slot, hardware-io could not re-bind, and the tank went unmonitored
for ten hours.

These tests exercise the predicate directly. No network, no environment.
"""

from __future__ import annotations

import pytest

from conftest import PRODUCTION_DATABASE, EndpointVerdict, classify_endpoint


@pytest.mark.parametrize(
    "url",
    [
        "nats://localhost:4222",
        "nats://127.0.0.1:4222",
        "postgresql+asyncpg://u:p@localhost:5432/bellasreef_test",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/bellasreef_test",
        "http://localhost:8428",
        "http://[::1]:8428",
    ],
)
def test_loopback_endpoints_are_allowed(url: str) -> None:
    assert classify_endpoint(url) == EndpointVerdict.ALLOWED


@pytest.mark.parametrize(
    "url",
    [
        "nats://bellasreef.local:4222",
        "postgresql+asyncpg://u:p@bellasreef.local:5432/bellasreef_test",
        "http://bellasreef.local:8428",
        "nats://192.168.1.50:4222",
        "http://hub.example.com:8428",
    ],
)
def test_remote_endpoints_are_refused(url: str) -> None:
    """Anything not on this machine is somebody's live hub until proven otherwise."""
    assert classify_endpoint(url) == EndpointVerdict.REMOTE


def test_the_production_database_is_refused_even_on_loopback() -> None:
    """Covers the one case loopback does not: running the suite ON the hub.

    There, the hub's own Postgres *is* localhost, so the host check passes and
    the database name is the only thing left to notice.
    """
    url = f"postgresql+asyncpg://u:p@localhost:5432/{PRODUCTION_DATABASE}"
    assert classify_endpoint(url) == EndpointVerdict.PRODUCTION_DATABASE


def test_a_test_database_on_loopback_is_fine() -> None:
    url = "postgresql+asyncpg://u:p@localhost:5432/bellasreef_test"
    assert classify_endpoint(url) == EndpointVerdict.ALLOWED


def test_an_unparseable_endpoint_is_refused_rather_than_waved_through() -> None:
    """A guard that fails open is not a guard."""
    assert classify_endpoint("not a url at all") == EndpointVerdict.REMOTE

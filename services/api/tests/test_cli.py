# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The operator CLI, driven the way an operator drives it.

Through ``main()`` with real argv. That is the whole point of this module: the
recovery tests that existed before it called ``Store.open_pairing_window()``
directly, which skips argparse, the DSN check, the engine lifecycle, everything
printed to the terminal, and the audit emission. The command could have been
broken in any of those and the suite would have stayed green, which is the same
shape of gap as a journey test holding both sides of a two-party conversation.

Needs `BELLASREEF_TEST_DATABASE_URL`. The audit sink is faked, because what is
under test is whether the CLI asks for the event at all and what it says when
the answer is no.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Coroutine
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest
from bellasreef_api import cli
from bellasreef_api.store import Store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")

NATS = "nats://127.0.0.1:4222"


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class FakeSink:
    """Stands in for :class:`NatsAuditSink`, recording instead of publishing.

    ``failures`` is the contract the CLI reads to decide whether to shout, so it
    is modelled rather than assumed: :class:`DeadSink` below is the same class
    with the answer flipped.
    """

    published: ClassVar[list[tuple[str, dict[str, Any]]]] = []
    sources: ClassVar[list[str]] = []
    fails = False

    def __init__(self, url: str, *, source: str = "api") -> None:
        self.url = url
        self.failures = 0
        FakeSink.sources.append(source)

    async def __call__(self, event: str, detail: dict[str, Any], category: str = "auth") -> None:
        if self.fails:
            self.failures += 1
            return
        FakeSink.published.append((event, detail))

    async def close(self) -> None:
        return None

    @classmethod
    def events(cls) -> list[str]:
        return [event for event, _ in cls.published]

    @classmethod
    def detail_for(cls, event: str) -> dict[str, Any]:
        return next(detail for name, detail in cls.published if name == event)


class DeadSink(FakeSink):
    fails = True


def engine_scoped[T](dsn: str, work: Callable[[AsyncEngine], Coroutine[Any, Any, T]]) -> T:
    """One engine per call, disposed inside the loop that made it.

    Every helper here is a separate ``asyncio.run``, and an asyncpg pool holds
    connections bound to the loop that opened them. Reusing one engine across
    calls fails with "attached to a different loop" somewhere unrelated, so it
    is not reused.
    """

    async def scenario() -> T:
        engine = create_async_engine(dsn, future=True)
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return run(scenario)


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> str:
    """A truncated database, a working audit sink, and the CLI's environment."""
    FakeSink.published = []
    FakeSink.sources = []
    monkeypatch.setattr("bellasreef_api.audit.NatsAuditSink", FakeSink)
    monkeypatch.setenv("BELLASREEF_DATABASE_URL", os.environ[_PG])
    monkeypatch.setenv("BELLASREEF_NATS_URL", NATS)

    dsn = os.environ[_PG]

    async def truncate(engine: AsyncEngine) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE pairing_requests, pairing_windows, paired_clients CASCADE")
            )

    engine_scoped(dsn, truncate)
    return dsn


@pytest.fixture
def fresh_db_env(hub: str) -> str:
    """``hub``, with ``hub_identity``'s setup columns reset to never-set-up.

    ``hub`` truncates the pairing tables but ``hub_identity`` is a singleton
    that is never truncated — otherwise the hub would mint a new identity mid
    suite. The setup-code tests need that row in a known state rather than
    whatever the previous test in the run left it in, so this seeds the row
    (``Store.hub_id()``, lazy like everything else that touches it) and then
    clears both setup columns explicitly.
    """

    async def reset(engine: AsyncEngine) -> None:
        await Store(engine).hub_id()
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE hub_identity SET setup_completed_at = NULL, setup_code_hash = NULL")
            )

    engine_scoped(hub, reset)
    return hub


@pytest.fixture
def paired_db_env(hub: str) -> str:
    """``fresh_db_env``, then ``complete_setup()`` — the post-adoption state."""

    async def complete(engine: AsyncEngine) -> None:
        store = Store(engine)
        await store.hub_id()
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE hub_identity SET setup_completed_at = NULL, setup_code_hash = NULL")
            )
        await store.complete_setup()

    engine_scoped(hub, complete)
    return hub


def add_client(dsn: str, name: str) -> tuple[UUID, str]:
    return engine_scoped(dsn, lambda engine: Store(engine).create_client(name))


def client_row(dsn: str, client_id: UUID) -> dict[str, Any]:
    async def read(engine: AsyncEngine) -> dict[str, Any]:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT revoked_at, refresh_token_hash FROM paired_clients WHERE id = :id"
                    ),
                    {"id": client_id},
                )
            ).mappings()
            first = row.first()
            assert first is not None
            return dict(first)

    return engine_scoped(dsn, read)


def token_still_works(dsn: str, refresh: str) -> bool:
    return (
        engine_scoped(dsn, lambda engine: Store(engine).client_for_refresh_token(refresh))
        is not None
    )


def window_count(dsn: str) -> int:
    async def read(engine: AsyncEngine) -> int:
        async with engine.connect() as conn:
            return int(
                (await conn.execute(text("SELECT count(*) FROM pairing_windows"))).scalar_one()
            )

    return engine_scoped(dsn, read)


# ---------------------------------------------------------------------- pair


def test_pair_opens_a_window_and_audits_it(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The fire escape, exercised as typed rather than as called."""
    assert cli.main(["pair", "--ttl", "60"]) == 0

    out = capsys.readouterr()
    assert "Pairing window open" in out.out
    assert window_count(hub) == 1

    assert FakeSink.events() == ["pair.window_opened"]
    detail = FakeSink.detail_for("pair.window_opened")
    assert detail["ttl_s"] == 60
    assert UUID(detail["window_id"])
    # The actor on every CLI event. Nothing else distinguishes a window opened
    # from the terminal from one opened by the API, which cannot open one.
    assert FakeSink.sources == ["bellasreef-cli"]


def test_pair_output_carries_the_ux6_sentence(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    """UX-6, 2026-08-14 iOS review: an operator who opens a second window while
    a code is already showing in the app needs to be told the old one is dead
    weight, not silently left to guess."""
    assert cli.main(["pair", "--ttl", "60"]) == 0
    assert "cancel and pair again" in capsys.readouterr().out


def test_pair_json_is_machine_readable(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["pair", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["expires_in_s"] > 0
    assert payload["clients_ever"] == 0


def test_pair_refuses_a_ttl_of_zero(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["pair", "--ttl", "0"]) == 2
    assert window_count(hub) == 0
    assert FakeSink.published == []


def test_pair_shouts_when_the_audit_sink_is_not_configured(
    hub: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression for the silent no-op.

    `BELLASREEF_NATS_URL` lives in a systemd environment file, so an operator who
    SSHes in and runs this command has it unset by default. It used to open the
    window, skip the audit event, print the identical success banner and exit 0.
    The one scenario this command exists for was the one that recorded nothing,
    and said nothing about it.
    """
    monkeypatch.delenv("BELLASREEF_NATS_URL")

    assert cli.main(["pair"]) == 0

    out = capsys.readouterr()
    assert window_count(hub) == 1, "the window must still open; audit is best effort"
    assert "Pairing window open" in out.out
    assert "NOT recorded" in out.err
    assert "BELLASREEF_NATS_URL" in out.err
    assert FakeSink.published == []


def test_pair_shouts_when_the_audit_sink_is_unreachable(
    hub: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configured and down is the same hole as not configured at all."""
    monkeypatch.setattr("bellasreef_api.audit.NatsAuditSink", DeadSink)

    assert cli.main(["pair"]) == 0

    out = capsys.readouterr()
    assert window_count(hub) == 1
    assert "NOT recorded" in out.err
    assert NATS in out.err


def test_a_missing_dsn_is_refused_before_anything_happens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BELLASREEF_DATABASE_URL", raising=False)
    assert cli.main(["pair"]) == 2
    assert "BELLASREEF_DATABASE_URL" in capsys.readouterr().err


# -------------------------------------------------------------------- revoke


def test_revoke_by_id(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    client_id, refresh = add_client(hub, "David's iPhone")

    assert cli.main(["revoke", str(client_id)]) == 0

    row = client_row(hub, client_id)
    assert row["revoked_at"] is not None
    assert row["refresh_token_hash"] is None
    # The credential is dead, not merely flagged.
    assert not token_still_works(hub, refresh)

    assert FakeSink.events() == ["client.revoked"]
    detail = FakeSink.detail_for("client.revoked")
    assert detail["client_id"] == str(client_id)
    assert detail["client_name"] == "David's iPhone"
    assert detail["revoked_via"] == "cli"
    assert FakeSink.sources == ["bellasreef-cli"]

    assert "Revoked David's iPhone" in capsys.readouterr().out


def test_revoke_by_name(hub: str) -> None:
    client_id, _ = add_client(hub, "David's iPhone")

    assert cli.main(["revoke", "David's iPhone"]) == 0

    assert client_row(hub, client_id)["revoked_at"] is not None
    assert FakeSink.detail_for("client.revoked")["client_id"] == str(client_id)


def test_revoke_by_name_ignores_case(hub: str) -> None:
    """A name typed off a screen at 1am is not a case-sensitive identifier."""
    client_id, _ = add_client(hub, "David's iPhone")

    assert cli.main(["revoke", "david's iphone"]) == 0
    assert client_row(hub, client_id)["revoked_at"] is not None


def test_an_ambiguous_name_revokes_nothing(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Two phones called "iPhone" is the expected case, not the exotic one.

    `UIDevice.current.name` returns the model on iOS 16+ without an entitlement
    this project does not declare, so every iOS device pairs under the same
    name. Guessing between them revokes tank control at the moment somebody is
    already having a bad day.
    """
    first, _ = add_client(hub, "iPhone")
    second, _ = add_client(hub, "iPhone")

    assert cli.main(["revoke", "iPhone"]) == 2

    err = capsys.readouterr().err
    assert str(first) in err and str(second) in err
    assert client_row(hub, first)["revoked_at"] is None
    assert client_row(hub, second)["revoked_at"] is None
    assert FakeSink.published == [], "a refusal is not an event"


def test_an_unknown_client_is_reported_not_invented(
    hub: str, capsys: pytest.CaptureFixture[str]
) -> None:
    add_client(hub, "David's iPhone")

    assert cli.main(["revoke", str(uuid4())]) == 2
    assert cli.main(["revoke", "nobody's phone"]) == 2

    err = capsys.readouterr().err
    assert "--list" in err
    assert FakeSink.published == []


def test_a_typo_in_an_id_never_falls_through_to_a_name(hub: str) -> None:
    """An id that matches nothing matches nothing. It does not become a search."""
    client_id, _ = add_client(hub, "David's iPhone")

    assert cli.main(["revoke", str(uuid4())]) == 2
    assert client_row(hub, client_id)["revoked_at"] is None


def test_revoking_twice_says_so(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    client_id, _ = add_client(hub, "David's iPhone")
    assert cli.main(["revoke", str(client_id)]) == 0
    first_revoked_at = client_row(hub, client_id)["revoked_at"]

    assert cli.main(["revoke", str(client_id)]) == 2

    assert client_row(hub, client_id)["revoked_at"] == first_revoked_at
    assert FakeSink.events() == ["client.revoked"], "no second event for a second attempt"
    assert "already revoked" in capsys.readouterr().err


def test_revoke_shouts_when_the_audit_sink_is_not_configured(
    hub: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason this subcommand exists at all.

    It is here to retire revoking in SQL, which is how a `client.revoked` row
    went missing on 2026-08-12. A revoke that can silently skip its own audit
    event has recreated the thing it replaced.
    """
    monkeypatch.delenv("BELLASREEF_NATS_URL")
    client_id, _ = add_client(hub, "David's iPhone")

    assert cli.main(["revoke", str(client_id)]) == 0

    out = capsys.readouterr()
    assert client_row(hub, client_id)["revoked_at"] is not None
    assert "client.revoked" in out.err
    assert "NOT recorded" in out.err


def test_revoke_shouts_when_the_audit_sink_is_unreachable(
    hub: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("bellasreef_api.audit.NatsAuditSink", DeadSink)
    client_id, _ = add_client(hub, "David's iPhone")

    assert cli.main(["revoke", str(client_id)]) == 0

    assert client_row(hub, client_id)["revoked_at"] is not None
    assert "NOT recorded" in capsys.readouterr().err


# ---------------------------------------------------------------------- list


def test_list_shows_every_client_and_revokes_nothing(
    hub: str, capsys: pytest.CaptureFixture[str]
) -> None:
    live, _ = add_client(hub, "David's iPhone")
    dead, _ = add_client(hub, "Old iPad")
    assert cli.main(["revoke", str(dead)]) == 0
    capsys.readouterr()

    assert cli.main(["revoke", "--list"]) == 0

    out = capsys.readouterr().out
    assert str(live) in out and str(dead) in out
    assert "David's iPhone" in out and "Old iPad" in out
    assert "REVOKED" in out
    assert client_row(hub, live)["revoked_at"] is None


def test_list_json_carries_the_fields_a_script_needs(
    hub: str, capsys: pytest.CaptureFixture[str]
) -> None:
    client_id, _ = add_client(hub, "David's iPhone")

    assert cli.main(["revoke", "--list", "--json"]) == 0

    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == [str(client_id)]
    assert rows[0]["active"] is True
    assert rows[0]["revoked_at"] is None
    assert rows[0]["created_at"]


def test_list_on_a_hub_nobody_has_paired_with(hub: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["revoke", "--list"]) == 0
    assert "No client has ever paired" in capsys.readouterr().out


def test_revoke_with_no_target_asks_rather_than_guesses(
    hub: str, capsys: pytest.CaptureFixture[str]
) -> None:
    add_client(hub, "David's iPhone")

    assert cli.main(["revoke"]) == 2

    assert "--list" in capsys.readouterr().err
    assert FakeSink.published == []


# ----------------------------------------------------------------- setup-code

#: SETUP_ALPHABET is Crockford base32 minus 0/O/1/I: digits 2-9, letters A-H,
#: J, K, M, N, P-T, V-Z (I, L, O, U excluded). Adapted from the brief's
#: `[2-9A-HJ-NP-Z]` to the actual alphabet in security.py.
_SETUP_CODE = re.compile(r"\b[2-9A-HJKMNPQRSTVWXYZ]{4}-[2-9A-HJKMNPQRSTVWXYZ]{4}\b")


def test_setup_code_mints_in_setup_mode(
    fresh_db_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["setup-code"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _SETUP_CODE.search(out)
    assert "Open the Bella's Reef app" in out


def test_setup_code_rotates(fresh_db_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["setup-code"])
    first = capsys.readouterr().out
    cli.main(["setup-code"])
    second = capsys.readouterr().out
    assert first != second  # old code is invalid now; only the new hash stored


def test_setup_code_after_setup_is_informational(
    paired_db_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["setup-code"])
    out = capsys.readouterr().out
    assert rc == 0 and "Setup is complete" in out

# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The version the hub advertises is the version the hub speaks.

It drifted. `CONTRACTS_VERSION` was a string literal in `app.py`, contracts went
to 3.1.0 for the silence class, and the constant stayed at 3.0.0 — so
`/api/v1/info` told every client it spoke an older contract than it did, and the
same string was being stamped into every backup manifest.

That is a worse failure than it sounds. The version is how a client decides
whether it can talk to this hub at all, and a hand-maintained copy of a number
that lives somewhere else has exactly one behaviour over time.

So it is derived from the installed package now, and this test exists to stop
anyone helpfully turning it back into a literal.
"""

from __future__ import annotations

from importlib.metadata import version

from bellasreef_api.app import CONTRACTS_VERSION


def test_the_advertised_version_is_the_installed_one() -> None:
    assert CONTRACTS_VERSION == version("bellasreef-contracts"), (
        "the hub is advertising a contracts version it does not have installed. "
        "CONTRACTS_VERSION must be derived from package metadata, never written "
        "out by hand — it drifted exactly that way once already."
    )


def test_it_is_not_a_hardcoded_literal() -> None:
    """Reads the source, because equality alone cannot catch a lucky literal.

    A hand-written "3.1.0" passes the test above right up until the next bump,
    which is precisely how the drift happened the first time. This asserts the
    *mechanism*, not the value.
    """
    from pathlib import Path

    import bellasreef_api.app as app_module

    source = Path(app_module.__file__).read_text()
    line = next(raw for raw in source.splitlines() if raw.startswith("CONTRACTS_VERSION"))
    assigned = line.split("=", 1)[1].strip()
    assert assigned.startswith("version("), (
        f"CONTRACTS_VERSION looks hand-written: {line!r}. It must be "
        "importlib.metadata.version('bellasreef-contracts') — a literal passes "
        "the equality test above right up until the next bump, which is exactly "
        "how it drifted the first time."
    )

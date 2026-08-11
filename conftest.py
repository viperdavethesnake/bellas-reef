# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""A skip for a missing environment fails the run.

The integration tests guard themselves with ``skipif(not os.environ.get(...))``,
which is the right shape — they genuinely cannot run without Postgres or NATS —
but it makes "the gate passed" mean two different things depending on the shell
it was run from. That is not hypothetical: a control-authority change went to CI
green locally with ``BELLASREEF_TEST_NATS_URL`` unset, the tests that would have
caught the missing seed silently skipped, and CI found it instead.

A skip has to be a decision, not a side effect. Setting
``BELLASREEF_ALLOW_ENV_SKIPS=1`` is the decision — for working offline, on a
plane, with no hub — and it has to be typed every time, which is exactly the
friction that makes it visible.

This is deliberately not a check that the variables are set. It reports what was
actually skipped and why, so the failure names the tests that did not run rather
than the configuration that was missing.
"""

from __future__ import annotations

import enum
import ipaddress
import os
from typing import Any
from urllib.parse import urlsplit

import pytest

# --------------------------------------------------------- environment boundary
#
# Integration tests never connect to the hub. This is the structural half of
# that rule; CLAUDE.md carries the prose.
#
# The reasoning is not "be careful with production". Durables are shared broker
# state and BR_CMD is a workqueue, which permits exactly ONE consumer per filter
# subject. A test that binds a durable on the hub's NATS is therefore competing
# for the hub's own slot by construction — there is no careful way to do it. On
# 2026-08-10 a leaked test durable held that slot, hardware-io could not
# re-bind, and the tank went unmonitored for ten hours.
#
# Loopback is the whole test: dev containers and CI services are on loopback,
# the hub is not.

#: The live database. Refused even on loopback, which is the case that matters
#: if the suite is ever run on the Pi itself.
PRODUCTION_DATABASE = "bellasreef"

#: Endpoints the boundary applies to.
_TEST_ENDPOINT_VARS = (
    "BELLASREEF_TEST_DATABASE_URL",
    "BELLASREEF_TEST_NATS_URL",
    "BELLASREEF_TEST_VM_URL",
)


class EndpointVerdict(enum.Enum):
    ALLOWED = "allowed"
    REMOTE = "remote"
    PRODUCTION_DATABASE = "production-database"


def classify_endpoint(url: str) -> EndpointVerdict:
    """Decide whether a test endpoint is on this machine and safe to touch.

    Fails closed: anything unparseable is REMOTE. A guard that waves through
    what it cannot read is decoration.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return EndpointVerdict.REMOTE

    if not host:
        return EndpointVerdict.REMOTE

    if host not in {"localhost", "ip6-localhost"}:
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return EndpointVerdict.REMOTE
        except ValueError:
            # A name that is not "localhost" and not an IP. Resolving it would
            # make the verdict depend on DNS, which is exactly the kind of
            # answer that changes between runs.
            return EndpointVerdict.REMOTE

    database = parts.path.lstrip("/")
    if database == PRODUCTION_DATABASE:
        return EndpointVerdict.PRODUCTION_DATABASE

    return EndpointVerdict.ALLOWED


def pytest_configure(config: Any) -> None:
    """Refuse to start if any test endpoint points off this machine."""
    offenders: list[tuple[str, str, EndpointVerdict]] = []
    for var in _TEST_ENDPOINT_VARS:
        url = os.environ.get(var)
        if not url:
            continue
        verdict = classify_endpoint(url)
        if verdict is not EndpointVerdict.ALLOWED:
            offenders.append((var, url, verdict))

    if not offenders:
        return

    lines = [
        "",
        "Integration test endpoints must be on loopback. Refusing to run.",
        "",
    ]
    for var, url, verdict in offenders:
        if verdict is EndpointVerdict.PRODUCTION_DATABASE:
            why = f"targets the production database {PRODUCTION_DATABASE!r}"
        else:
            why = "is not on this machine"
        lines += [f"  {var}", f"    {url}", f"    -> {why}", ""]
    lines += [
        "These suites bind durable consumers. BR_CMD is a workqueue and permits",
        "one consumer per filter subject, so pointing them at the hub competes",
        "with the hub for its own slot. That cost ten hours of lost monitoring",
        "on 2026-08-10.",
        "",
        "Run against loopback dev containers, or accept that these are not",
        "being checked here and let CI check them:",
        "",
        f"  {_ALLOW}=1 ./scripts/check.sh",
        "",
    ]
    raise pytest.UsageError("\n".join(lines))


#: Skip reasons are matched on this. Every environment guard in this repo is
#: written as "<VAR> not set…", so the substring is the contract between those
#: guards and this hook — see the `reason=` arguments in db/tests/helpers.py and
#: the service test modules.
_ENV_SKIP_MARKER = "not set"

_ALLOW = "BELLASREEF_ALLOW_ENV_SKIPS"

_skipped: list[tuple[str, str]] = []


def pytest_runtest_logreport(report: Any) -> None:
    if not report.skipped or report.when != "setup":
        return
    # (path, lineno, "Skipped: <reason>") for a skipif-driven skip.
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    if _ENV_SKIP_MARKER in reason:
        _skipped.append((report.nodeid, reason))


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if not _skipped or os.environ.get(_ALLOW):
        return

    unique = sorted({reason for _, reason in _skipped})
    print("\n" + "=" * 72)
    print(f"{len(_skipped)} test(s) skipped for a missing environment:")
    for reason in unique:
        count = sum(1 for _, r in _skipped if r == reason)
        print(f"  {count:>4}  {reason}")
    print()
    print("These tests did not run, so this result does not mean what it looks")
    print("like. Provide the environment, or say so explicitly:")
    print(f"  {_ALLOW}=1  — I accept that these are not being checked")
    print("=" * 72)

    session.exitstatus = 1

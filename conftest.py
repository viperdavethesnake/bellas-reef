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

import os
from typing import Any

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

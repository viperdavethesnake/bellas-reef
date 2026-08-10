# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Whether the host clock can be believed.

One predicate, shared, because "is the clock trustworthy" must mean the same
thing everywhere it gates something. This board has no RTC battery: after a
power cut it boots on whatever `fake-hwclock` saved and stays wrong until chrony
catches up.

Anything that stamps or compares a deadline has to consult this — command
expiry in the engine, override deadlines wherever they are created. A second
implementation that drifted from this one would be worse than none, because the
two gates would disagree about the same moment.
"""

from __future__ import annotations

import os
import subprocess

__all__ = ["ASSUME_TRUSTED_ENV", "clock_is_trusted"]

#: Explicit opt-in for environments with no `timedatectl` — inside a container,
#: where the host guarantees trust by ordering the stack After=time-sync.target.
#: Explicit on purpose: silently assuming a good clock is the failure being
#: guarded against.
ASSUME_TRUSTED_ENV = "BELLASREEF_ASSUME_CLOCK_TRUSTED"


def clock_is_trusted() -> bool:
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return os.environ.get(ASSUME_TRUSTED_ENV) == "1"
    if out.returncode != 0:
        return os.environ.get(ASSUME_TRUSTED_ENV) == "1"
    return out.stdout.strip().lower() == "yes"

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

import ctypes
import ctypes.util
import os
import subprocess

from bellasreef_service.logging import get_logger

__all__ = ["ASSUME_TRUSTED_ENV", "clock_is_trusted", "kernel_clock_synchronised"]

log = get_logger(__name__)

#: Explicit opt-in for environments where NEITHER oracle answers — no
#: `timedatectl` and no working `adjtimex(2)`. As of the 2026-08-23 flip that
#: is no longer "inside a container" (the kernel oracle decides there now,
#: proven live on the Pi); it is the dev-machine case, e.g. macOS, where
#: libc has no adjtimex. `deploy/compose.yaml` no longer sets this — only
#: `.env.local` on the Mac does. Explicit on purpose: silently assuming a
#: good clock is the failure being guarded against.
ASSUME_TRUSTED_ENV = "BELLASREEF_ASSUME_CLOCK_TRUSTED"

#: adjtimex(2) return value meaning "clock not synchronised" (TIME_ERROR, aka
#: TIME_BAD in glibc's older name for the same constant).
_TIME_ERROR = 5


class _Timex(ctypes.Structure):
    """Mirrors Linux's ``struct timex`` (see ``man 2 adjtimex``).

    aarch64 and x86_64 share this layout — ``long`` is 8 bytes on both, so one
    definition covers the Pi and any amd64 dev/CI box. Not defined, and not
    trustworthy, on a kernel where that is not true; ``kernel_clock_synchronised``
    only ever runs this against a real Linux libc.
    """

    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_pad", ctypes.c_int * 11),
    ]


def _load_libc() -> ctypes.CDLL | None:
    try:
        name = ctypes.util.find_library("c")
        return ctypes.CDLL(name, use_errno=True) if name else None
    except OSError:
        return None


#: Loaded once at import time. None on a platform with no ``libc.so`` name
#: resolvable this way (never observed) — the per-call ``hasattr`` check below
#: is what actually excludes macOS, where libc exists but has no adjtimex.
_libc = _load_libc()

if _libc is not None and hasattr(_libc, "adjtimex"):
    # ctypes' untyped default (treat every arg as a C int and the return as a
    # C int) happens to work here, but this symbol feeds a safety-adjacent
    # decision even in shadow mode — pin the real signature rather than lean
    # on the default. Guarded: on macOS (or any libc without adjtimex) this
    # block never runs, and kernel_clock_synchronised()'s own hasattr check
    # is what excludes it there.
    _libc.adjtimex.restype = ctypes.c_int
    _libc.adjtimex.argtypes = [ctypes.POINTER(_Timex)]


def kernel_clock_synchronised() -> bool | None:
    """Ask the kernel, not systemd: adjtimex(2) is visible from inside a
    container, where timedatectl is not. Returns None where the syscall is
    unavailable (dev Mac) or fails — unknown is an answer, a guess is not.

    Promoted into ``clock_is_trusted()``'s decision chain 2026-08-23 (David's
    ruling to flip): it ran in shadow mode only from task 4 until then, and
    was proven live in-container on the Pi the day of the flip (returned
    True, no seccomp block). It now decides trust for every container, where
    timedatectl is unreachable — the production path.
    """
    if _libc is None or not hasattr(_libc, "adjtimex"):
        return None
    buf = _Timex()
    buf.modes = 0  # read-only query
    try:
        ret: int = _libc.adjtimex(ctypes.byref(buf))
    except Exception:
        return None
    if ret < 0:
        return None
    return bool(ret != _TIME_ERROR)


#: Latches the kernel oracle's value for as long as it stays in disagreement
#: with ``clock_is_trusted()``'s own answer, so a steady disagreement logs
#: once rather than once per refresh cycle forever. None means "not currently
#: disagreeing" — reset on every agreement (or unknown reading), which is what
#: lets a later disagreement, even one that flips back to a previously-logged
#: value, log again rather than being mistaken for the same standing one.
_last_shadow: bool | None = None


def _log_shadow_disagreement(*, kernel: bool | None, returned: bool) -> None:
    global _last_shadow
    if kernel is None or kernel == returned:
        _last_shadow = None
        return
    if _last_shadow is kernel:
        return
    _last_shadow = kernel
    log.warning(
        "kernel clock oracle disagrees with timedatectl (timedatectl's answer is used here; "
        "the oracle only decides when timedatectl is unreachable)",
        extra={"event": "clock_shadow_disagreement", "kernel": kernel, "returned": returned},
    )


def _timedatectl_says_trusted() -> bool | None:
    """Ask systemd. Returns ``None`` when timedatectl itself is unreachable —
    missing binary, timeout, or a nonzero exit — as distinct from a real
    answer, so ``clock_is_trusted()`` below can tell "no systemd here" (every
    container) from "systemd says no" (a host that has drifted)."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip().lower() == "yes"


def clock_is_trusted() -> bool:
    """Decision chain, promoted out of shadow mode 2026-08-23 (David
    delegated the flip; the kernel oracle ran in shadow since task 4 and was
    proven live in-container on the Pi that day — returned True, no seccomp
    block on adjtimex).

    1. ``timedatectl`` answers -> its answer wins (host runs, CI runners with
       systemd — there is a real ``NTPSynchronized`` property to read).
    2. ``timedatectl`` is unreachable (every container: there is no systemd
       inside one) and the kernel oracle answers -> the oracle decides. This
       is the production path now.
    3. Neither answers (macOS dev: no timedatectl, and libc has no adjtimex)
       -> the ``BELLASREEF_ASSUME_CLOCK_TRUSTED`` env override, which is now
       only a dev-machine fallback — ``.env.local`` on the Mac is the only
       place still setting it; `deploy/compose.yaml` no longer does.
    4. Nothing answers -> False.

    The failure direction is by design: this board has no RTC battery, so an
    oracle that says "not synchronised" (or nothing answering at all) must
    refuse rather than guess — commands come back ``rejected_clock`` and
    lighting holds stop advancing (dark, not lit wrong) until chrony syncs.

    Shadow-disagreement logging is kept only on the timedatectl-decides
    branch (step 1) — that is the only branch where the oracle is a *second*
    opinion rather than *the* decision, so it is the only branch with
    anything left to shadow.
    """
    timedatectl_result = _timedatectl_says_trusted()
    if timedatectl_result is not None:
        _log_shadow_disagreement(kernel=kernel_clock_synchronised(), returned=timedatectl_result)
        return timedatectl_result

    kernel_result = kernel_clock_synchronised()
    if kernel_result is not None:
        return kernel_result

    return os.environ.get(ASSUME_TRUSTED_ENV) == "1"

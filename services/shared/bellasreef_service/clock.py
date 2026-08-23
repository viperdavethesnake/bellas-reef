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

#: Explicit opt-in for environments with no `timedatectl` — inside a container,
#: where the host guarantees trust by ordering the stack After=time-sync.target.
#: Explicit on purpose: silently assuming a good clock is the failure being
#: guarded against.
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

    Shadow-mode oracle only (2026-08-23, task 4): nothing gates on this yet.
    ``clock_is_trusted()`` below logs when the two disagree so the oracle can
    be trusted against real logs before enforcement is ever switched to it —
    see the PR-body note for the flip David has yet to rule on.
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
        "kernel clock oracle disagrees with clock_is_trusted() (shadow mode, not enforced)",
        extra={"event": "clock_shadow_disagreement", "kernel": kernel, "returned": returned},
    )


def _timedatectl_says_trusted() -> bool:
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


def clock_is_trusted() -> bool:
    result = _timedatectl_says_trusted()
    _log_shadow_disagreement(kernel=kernel_clock_synchronised(), returned=result)
    return result

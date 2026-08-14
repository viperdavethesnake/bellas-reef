# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Process liveness.

The kernel watchdog on this board (`/dev/watchdog0`, systemd
`RuntimeWatchdogUSec=1min`) catches a wedged *kernel*. It does nothing about a
Python process whose event loop has deadlocked while the kernel hums along —
which is the failure that actually leaves a heater on.

``LivenessGuard`` is that mechanism, and under the locked runtime it is the
only one. A watchdog **thread** (not a task — a stalled loop cannot run its own
rescue) watches a heartbeat emitted from inside the supervisor loop and calls
``os._exit`` if it goes stale, so the container exits and
``restart: unless-stopped`` brings it back.

.. note::

   An ``sd_notify`` implementation lived here until 2026-08-14, pinging
   ``WATCHDOG=1`` for the host systemd units that supervised these services
   between 2026-08-10 and 2026-08-13. Containers-only closed that era, and
   under Compose there is no ``NOTIFY_SOCKET`` — so it was a branch that could
   not execute on any deployed path. Deleted rather than kept: a dormant
   second answer to "is this process alive" reads as a supported option.

.. warning::

   **This is a genuine gap in the locked runtime, flagged rather than hidden.**
   CLAUDE.md locks Docker Compose as the runtime. Docker does **not** restart a
   container that is merely *unhealthy* — ``restart: unless-stopped`` acts on
   process *exit*. So a healthcheck alone cannot recover a hung loop in a
   container; something must make the process die. That is what
   ``LivenessGuard`` is for, and why it is not redundant with the healthcheck.
"""

from __future__ import annotations

import os
import threading
import time

from bellasreef_service.logging import get_logger

__all__ = ["LIVENESS_EXIT_CODE", "LivenessGuard"]

#: Exit code used when the liveness guard kills us. Deliberately distinct so a
#: post-incident `docker inspect` says WHICH mechanism fired:
#:
#:   0    clean stop
#:   70   liveness guard — the supervisor loop stalled  (EX_SOFTWARE)
#:   137  SIGKILL / OOM killer (128+9)
#:
#: Knowing a stall killed us rather than the OOM killer changes what you go
#: and look at next. `scripts/drill-restart.sh` asserts on exactly this value.
LIVENESS_EXIT_CODE = 70

log = get_logger(__name__)


class LivenessGuard:
    """Kills the process if the supervisor loop stops beating.

    Runs on a **thread**, not an asyncio task. A frozen event loop cannot run
    the task that would have rescued it — that is the entire point, and it is
    the mistake this class exists to avoid.

    ``os._exit`` is used deliberately: a hung loop means ``sys.exit`` and
    atexit handlers cannot be trusted to run, and the goal is for the process
    to be *gone* so the supervisor restarts it.
    """

    def __init__(
        self,
        *,
        timeout_s: float,
        on_exit: object = None,
        check_interval_s: float = 0.5,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        self._timeout_s = timeout_s
        self._check_interval_s = min(check_interval_s, timeout_s / 2)
        self._last_beat = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Injectable so tests can assert the decision without dying.
        self._exit = on_exit if callable(on_exit) else self._hard_exit

    @staticmethod
    def _hard_exit() -> None:  # pragma: no cover - process death
        os._exit(LIVENESS_EXIT_CODE)

    def beat(self) -> None:
        """Called from the supervisor loop. Cheap and thread-safe."""
        with self._lock:
            self._last_beat = time.monotonic()

    def age_s(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_beat

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="liveness-guard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._check_interval_s):
            age = self.age_s()
            if age > self._timeout_s:
                log.critical(
                    "supervisor loop stalled; terminating so the runtime restarts us",
                    extra={"stall_s": round(age, 3), "timeout_s": self._timeout_s},
                )
                self._exit()
                return

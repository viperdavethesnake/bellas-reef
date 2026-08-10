# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Process liveness.

The kernel watchdog on this board (`/dev/watchdog0`, systemd
`RuntimeWatchdogUSec=1min`) catches a wedged *kernel*. It does nothing about a
Python process whose event loop has deadlocked while the kernel hums along —
which is the failure that actually leaves a heater on.

Two mechanisms, because this service has two deployment paths and only one of
them can use `sd_notify`:

``SdNotifier``
    Under systemd with ``Type=notify`` + ``WatchdogSec=``, pinging
    ``WATCHDOG=1`` from inside the supervisor loop. If the loop stops turning,
    the pings stop, and systemd kills and restarts the unit.

``LivenessGuard``
    Under Docker, where there is no ``NOTIFY_SOCKET``. A watchdog **thread**
    (not a task — a stalled loop cannot run its own rescue) watches the same
    heartbeat and calls ``os._exit`` if it goes stale, so the container exits
    and ``restart: unless-stopped`` brings it back.

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
import socket
import threading
import time
from types import TracebackType
from typing import Self

from bellasreef_service.logging import get_logger

__all__ = ["LIVENESS_EXIT_CODE", "LivenessGuard", "SdNotifier", "watchdog_interval_s"]

#: Exit code used when the liveness guard kills us. Deliberately distinct so a
#: post-incident `docker inspect` or journal entry says WHICH mechanism fired:
#:
#:   0    clean stop
#:   70   liveness guard — the supervisor loop stalled  (EX_SOFTWARE)
#:   134  systemd watchdog SIGABRT (128+6)
#:   137  SIGKILL / OOM killer (128+9)
#:
#: Knowing a stall killed us rather than the OOM killer changes what you go
#: and look at next.
LIVENESS_EXIT_CODE = 70

log = get_logger(__name__)

_NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
_WATCHDOG_USEC_ENV = "WATCHDOG_USEC"
_WATCHDOG_PID_ENV = "WATCHDOG_PID"


def watchdog_interval_s(default: float = 5.0) -> float:
    """How often to ping, derived from systemd's ``WatchdogSec``.

    systemd's own guidance is to notify at **half** the configured interval, so
    a single missed cycle is not fatal but two are.
    """
    raw = os.environ.get(_WATCHDOG_USEC_ENV)
    if not raw:
        return default
    try:
        usec = int(raw)
    except ValueError:
        return default
    if usec <= 0:
        return default

    # WATCHDOG_PID guards against inheriting a parent's watchdog settings.
    pid = os.environ.get(_WATCHDOG_PID_ENV)
    if pid and pid != str(os.getpid()):
        return default

    return (usec / 1_000_000.0) / 2.0


class SdNotifier:
    """Minimal ``sd_notify`` client.

    A datagram to the socket in ``NOTIFY_SOCKET``. Deliberately not using the
    systemd Python bindings — this is a dozen lines and avoids a compiled
    dependency in an arm64 image.

    A no-op when ``NOTIFY_SOCKET`` is absent, so the same code runs unchanged
    under Docker and in tests.
    """

    def __init__(self, address: str | None = None) -> None:
        self._address = address if address is not None else os.environ.get(_NOTIFY_SOCKET_ENV)
        self._sock: socket.socket | None = None
        if self._address:
            # Python sockets are non-inheritable by default since 3.4, so no
            # SOCK_CLOEXEC needed — and that flag is Linux-only, which would
            # break type-checking and imports on the macOS dev machine.
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    @property
    def enabled(self) -> bool:
        return self._sock is not None and bool(self._address)

    def _send(self, message: str) -> None:
        if self._sock is None or not self._address:
            return
        # A leading NUL marks an abstract socket on Linux.
        path = "\0" + self._address[1:] if self._address.startswith("@") else self._address
        try:
            self._sock.sendto(message.encode(), path)
        except OSError:
            # Never let telling systemd we are alive be the thing that kills us.
            log.warning("sd_notify send failed", extra={"message": message}, exc_info=True)

    def ready(self) -> None:
        self._send("READY=1")

    def ping(self) -> None:
        self._send("WATCHDOG=1")

    def stopping(self) -> None:
        self._send("STOPPING=1")

    def status(self, text: str) -> None:
        self._send(f"STATUS={text}")

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


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

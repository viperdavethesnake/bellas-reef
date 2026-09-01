# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The hub machine's own vitals, read and published.

`/proc` loadavg/meminfo/uptime are system-wide even inside a container, and
`/sys` is the host's sysfs — measured in bellasreef-hardware-io-1 on coco
2026-08-31 (46.3 °C, host loadavg, host MemTotal), which is why this needs
no new mounts and no new privileges. Published on ``bellasreef.host.status``
retained last-value, snapshot only — the design and its deliberate
exclusions (no disk, no throttle flags, no VM history) are recorded in
docs/superpowers/specs/2026-08-31-hub-status-design.md.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from bellasreef_contracts import HostStatus

log = logging.getLogger(__name__)

SERVICE = "hardware-io"

#: A status page's freshness, not telemetry: fast enough that the app's
#: "last updated" stays believable, slow enough to be free.
HOST_STATUS_INTERVAL_S = 30.0

_THERMAL_DEFAULT = Path("/sys/class/thermal/thermal_zone0/temp")


class _HostStatusSpine(Protocol):
    async def publish_host_status(self, status: HostStatus) -> None: ...


class HostStatusReader:
    """Reads the host files into a HostStatus message.

    Paths are injectable for tests; the defaults are the container's own
    view, which per the module docstring is the host's.
    """

    def __init__(self, proc: Path = Path("/proc"), thermal: Path = _THERMAL_DEFAULT) -> None:
        self._proc = proc
        self._thermal = thermal

    def read(self) -> HostStatus:
        load_1m, load_5m, load_15m = (
            float(part) for part in (self._proc / "loadavg").read_text().split()[:3]
        )
        mem: dict[str, int] = {}
        for line in (self._proc / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                mem[key] = int(rest.split()[0])
        uptime_s = float((self._proc / "uptime").read_text().split()[0])
        return HostStatus(
            message_id=uuid4(),
            emitted_at=datetime.now(UTC),
            source=SERVICE,
            load_1m=load_1m,
            load_5m=load_5m,
            load_15m=load_15m,
            cpu_count=os.cpu_count() or 1,
            mem_total_kb=mem["MemTotal"],
            mem_available_kb=mem["MemAvailable"],
            temp_c=self._read_temp(),
            uptime_s=uptime_s,
        )

    def _read_temp(self) -> float | None:
        # Millidegrees, or None when this host has no readable thermal zone —
        # a real state the contract carries as None, never a fabricated 0.
        try:
            return int(self._thermal.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            return None


class HostStatusPublisher:
    """Publishes a fresh snapshot on its own cadence, best-effort.

    Driven from the main loop's tick (like sensor polling) rather than a
    separate task, so a stalled loop stops publishing — the same honesty rule
    as the heartbeat. A failure logs and consumes its slot: the loop also
    drives safety, and a spine outage must neither kill it nor turn the next
    tick into a hammer.
    """

    def __init__(
        self,
        spine: Callable[[], _HostStatusSpine | None],
        reader: HostStatusReader,
        interval_s: float = HOST_STATUS_INTERVAL_S,
    ) -> None:
        # A getter, not the spine itself: the publisher is built at service
        # init, before the spine has connected, and must survive it being
        # None without a null-check leaking into the main loop.
        self._spine = spine
        self._reader = reader
        self._interval_s = interval_s
        self._due = 0.0

    async def maybe_publish(self, now: float) -> None:
        if now < self._due:
            return
        spine = self._spine()
        if spine is None:
            return
        self._due = now + self._interval_s
        try:
            await spine.publish_host_status(self._reader.read())
        except Exception:
            log.warning("host status publish failed", exc_info=True)

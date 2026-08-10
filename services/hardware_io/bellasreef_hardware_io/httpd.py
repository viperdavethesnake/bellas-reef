# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Health and metrics endpoints.

A hand-rolled asyncio HTTP server rather than a web framework. This service's
job is to own hardware and fail safe; adding FastAPI and its dependency tree to
it — in the one container that gets `/dev` access — buys nothing. The API
service is where a framework belongs.

``/healthz`` reports **real** state. A health endpoint that always answers 200
is worse than none: it converts a hung process into a green dashboard.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from bellasreef_hardware_io.logging import get_logger

__all__ = ["Health", "HealthProbe", "MetricsServer"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Health:
    """What the service knows about itself."""

    healthy: bool
    reason: str
    loop_stall_s: float
    clock_trusted: bool
    actuators: int
    latched: tuple[str, ...]


HealthProbe = Callable[[], Health]


class MetricsServer:
    """Serves ``/healthz`` and ``/metrics``. Nothing else."""

    def __init__(
        self,
        *,
        probe: HealthProbe,
        registry: CollectorRegistry,
        host: str = "0.0.0.0",
        port: int = 9101,
    ) -> None:
        self._probe = probe
        self._registry = registry
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host=self._host, port=self._port)
        log.info("metrics server listening", extra={"host": self._host, "port": self._port})

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5.0)
            path = request.decode("latin-1").split(" ")[1] if b" " in request else "/"

            # Drain headers so the client sees a clean response.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            status, content_type, body = self._route(path)
            writer.write(
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode("latin-1")
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionResetError, IndexError, UnicodeDecodeError):
            pass  # a malformed probe request is not an event worth logging
        finally:
            writer.close()

    def _route(self, path: str) -> tuple[str, str, bytes]:
        base = path.split("?", 1)[0]
        if base == "/healthz":
            health = self._probe()
            body = json.dumps(asdict(health), default=list).encode()
            status = "200 OK" if health.healthy else "503 Service Unavailable"
            return status, "application/json", body
        if base == "/metrics":
            return "200 OK", CONTENT_TYPE_LATEST, generate_latest(self._registry)
        return "404 Not Found", "text/plain", b"not found\n"


async def probe_once(host: str, port: int, path: str) -> tuple[int, bytes]:
    """Tiny client used by tests and the container healthcheck."""
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()

    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    code = int(status_line.split(" ")[1])
    return code, body

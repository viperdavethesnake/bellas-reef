# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""hardware-io health and metrics endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

from bellasreef_hardware_io.app import HardwareIO
from bellasreef_service.httpd import probe_once


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class TestHealthAndMetrics:
    def test_health_is_green_when_running(self) -> None:
        async def scenario() -> tuple[int, bytes]:
            svc = HardwareIO(metrics_port=0, liveness_timeout_s=5.0)
            svc._clock_trusted = True
            await svc.httpd.start()
            assert svc.httpd._server is not None
            port = svc.httpd._server.sockets[0].getsockname()[1]
            try:
                return await probe_once("127.0.0.1", port, "/healthz")
            finally:
                await svc.httpd.stop()

        code, body = run(scenario)
        assert code == 200
        assert json.loads(body)["healthy"] is True

    def test_health_is_503_when_the_clock_is_not_trusted(self) -> None:
        """A green light on a wrong clock would be worse than no light."""

        async def scenario() -> tuple[int, bytes]:
            svc = HardwareIO(metrics_port=0)
            svc._clock_trusted = False
            await svc.httpd.start()
            assert svc.httpd._server is not None
            port = svc.httpd._server.sockets[0].getsockname()[1]
            try:
                return await probe_once("127.0.0.1", port, "/healthz")
            finally:
                await svc.httpd.stop()

        code, body = run(scenario)
        assert code == 503
        assert json.loads(body)["clock_trusted"] is False

    def test_metrics_are_prometheus_format(self) -> None:
        async def scenario() -> tuple[int, bytes]:
            svc = HardwareIO(metrics_port=0)
            svc.metrics.loop_beats.inc()
            await svc.httpd.start()
            assert svc.httpd._server is not None
            port = svc.httpd._server.sockets[0].getsockname()[1]
            try:
                return await probe_once("127.0.0.1", port, "/metrics")
            finally:
                await svc.httpd.stop()

        code, body = run(scenario)
        assert code == 200
        text = body.decode()
        assert "# TYPE bellasreef_loop_beats_total counter" in text
        assert "bellasreef_loop_beats_total 1.0" in text

    def test_unknown_path_is_404(self) -> None:
        async def scenario() -> tuple[int, bytes]:
            svc = HardwareIO(metrics_port=0)
            await svc.httpd.start()
            assert svc.httpd._server is not None
            port = svc.httpd._server.sockets[0].getsockname()[1]
            try:
                return await probe_once("127.0.0.1", port, "/admin")
            finally:
                await svc.httpd.stop()

        code, _ = run(scenario)
        assert code == 404


def threading_flag() -> Any:
    import threading

    return threading.Event()

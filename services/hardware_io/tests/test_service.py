# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Service skeleton: logging, liveness, health, metrics."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from bellasreef_hardware_io.app import HardwareIO
from bellasreef_hardware_io.httpd import probe_once
from bellasreef_hardware_io.logging import JsonFormatter, configure_logging
from bellasreef_hardware_io.watchdog import (
    LIVENESS_EXIT_CODE,
    LivenessGuard,
    SdNotifier,
    watchdog_interval_s,
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class TestJsonLogging:
    def _emit(self, **extra: Any) -> dict[str, Any]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter(service="hardware-io"))
        logger = logging.getLogger("test.json")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.info("hello", extra=extra)
        parsed: dict[str, Any] = json.loads(stream.getvalue().strip())
        return parsed

    def test_one_json_object_per_line(self) -> None:
        record = self._emit()
        assert record["msg"] == "hello"
        assert record["level"] == "INFO"
        assert record["service"] == "hardware-io"

    def test_timestamps_are_explicit_utc(self) -> None:
        """The one place you can later see the clock was wrong."""
        assert self._emit()["ts"].endswith("+00:00")

    def test_extra_context_is_preserved(self) -> None:
        record = self._emit(actuator_id="ato-pump", reason="heartbeat_timeout")
        assert record["actuator_id"] == "ato-pump"
        assert record["reason"] == "heartbeat_timeout"

    def test_unserialisable_values_do_not_break_the_log_call(self) -> None:
        """Logging is often reporting a problem; it must not become one."""
        record = self._emit(obj=object())
        assert "obj" in record

    def test_configure_replaces_handlers(self) -> None:
        logging.basicConfig()
        configure_logging(service="hardware-io")
        assert len(logging.getLogger().handlers) == 1


class TestLivenessGuard:
    def test_fires_when_beats_stop(self) -> None:
        fired = threading_flag()
        guard = LivenessGuard(timeout_s=0.15, on_exit=fired.set, check_interval_s=0.02)
        guard.start()
        try:
            assert not fired.wait(0.05), "must not fire while fresh"
            assert fired.wait(1.0), "must fire once beats stop"
        finally:
            guard.stop()

    def test_does_not_fire_while_beating(self) -> None:
        fired = threading_flag()
        guard = LivenessGuard(timeout_s=0.2, on_exit=fired.set, check_interval_s=0.02)
        guard.start()
        try:
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                guard.beat()
                time.sleep(0.02)
            assert not fired.is_set()
        finally:
            guard.stop()

    def test_runs_on_a_thread_so_a_stalled_loop_cannot_disable_it(self) -> None:
        """The point of the design.

        The guard watches from a thread; a frozen event loop cannot run the
        task that would have rescued it.
        """
        fired = threading_flag()

        async def scenario() -> None:
            guard = LivenessGuard(timeout_s=0.15, on_exit=fired.set, check_interval_s=0.02)
            guard.start()
            guard.beat()
            # Block the event loop exactly as a deadlock would.
            time.sleep(0.5)
            guard.stop()

        run(scenario)
        assert fired.is_set(), "a blocked event loop must still be detected"

    def test_rejects_non_positive_timeout(self) -> None:
        with pytest.raises(ValueError):
            LivenessGuard(timeout_s=0.0)

    def test_exit_code_is_distinct_from_other_death_modes(self) -> None:
        """Post-incident, you need to know WHICH mechanism killed the process.

        0 is a clean stop, 137 is SIGKILL/OOM, 134 is systemd's watchdog
        SIGABRT. A liveness kill must not be confusable with any of them.
        """
        assert LIVENESS_EXIT_CODE != 0
        assert LIVENESS_EXIT_CODE not in (134, 137)
        assert LIVENESS_EXIT_CODE == 70  # EX_SOFTWARE


class TestSdNotifier:
    def test_is_a_noop_without_a_notify_socket(self) -> None:
        """Same code path under Docker, where systemd is not present."""
        notifier = SdNotifier(address=None)
        assert notifier.enabled is False
        notifier.ready()
        notifier.ping()  # must not raise

    def test_interval_is_half_of_watchdog_usec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """systemd's guidance: notify at half the configured interval."""
        monkeypatch.setenv("WATCHDOG_USEC", "20000000")  # 20 s
        monkeypatch.delenv("WATCHDOG_PID", raising=False)
        assert watchdog_interval_s() == pytest.approx(10.0)

    def test_falls_back_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        assert watchdog_interval_s(default=7.0) == 7.0

    def test_ignores_a_watchdog_inherited_from_another_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "20000000")
        monkeypatch.setenv("WATCHDOG_PID", "999999")
        assert watchdog_interval_s(default=3.0) == 3.0


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
